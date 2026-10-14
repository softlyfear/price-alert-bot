"""Loguru configuration for the application process.

Provides a single entry point that configures the root loguru logger.
Nothing runs at import time; the caller decides when logging becomes
active (see PAB-033 for the intended call site in ``app.main``).
"""

import inspect
import logging
import sys
from collections.abc import Collection

from loguru import logger

_LOG_LEVEL = "INFO"
_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level> | {extra}"
)
_REDACTED = "***"


class _InterceptHandler(logging.Handler):
    """Forward standard ``logging`` records into loguru.

    The class identifies our own handlers on repeated ``setup_logging``
    calls, so they are replaced instead of duplicated.

    ``Handler.handle`` calls ``emit`` unguarded, so a malformed record (for
    example a ``%s`` format with too few arguments) must not raise into the
    code that logged it. On ``Exception`` one fallback ``ERROR`` record goes
    through loguru, hence through the sink's secret filter. It carries only
    the stdlib logger name and the error type, as ``{}`` arguments (never
    parsed as a template), plus the error's own traceback. The record's
    ``msg``/``args`` are not echoed: ``Handler.handleError`` would print them
    raw to ``stderr``, bypassing the filter. Known limitation: the fallback
    record does not carry the original ``record.exc_info`` when it was passed
    explicitly outside an ``except`` block (inside one, the original error
    is kept by the ``During handling...`` exception chain).
    ``BaseException`` subclasses outside ``Exception`` propagate.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame is not None and (
            depth == 0 or frame.f_code.co_filename == logging.__file__
        ):
            frame = frame.f_back
            depth += 1
        try:
            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )
        except Exception as error:
            logger.opt(depth=depth, exception=error).error(
                "Unformattable stdlib log record from {}: {}",
                record.name,
                type(error).__name__,
            )


def _install_interceptors() -> None:
    """Route the root and ``uvicorn`` stdlib loggers into loguru.

    The root handler is limited to ``WARNING`` (the threshold of
    ``logging.lastResort``) and the root level is left alone, as are foreign
    root handlers. ``uvicorn`` gets the interceptor instead of its own
    handlers, ``propagate`` stays ``False``; ``uvicorn.access`` is untouched.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _InterceptHandler)]:
        root.removeHandler(handler)
    root_handler = _InterceptHandler(level=logging.WARNING)
    root.addHandler(root_handler)

    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers = [_InterceptHandler()]
    uvicorn_logger.propagate = False


def setup_logging(*, redact: Collection[str] = ()) -> None:
    """Configure the root loguru logger for the process.

    Every record, from loguru and from standard ``logging`` (``WARNING`` and
    above on the root logger, everything ``uvicorn`` emits), goes through one
    sink that replaces each non-empty value of ``redact`` with ``***`` in the
    finished text: message, ``{extra}`` and traceback alike. Empty values are
    ignored (replacing ``""`` would insert the mask between all characters).
    The state is set by the last call.

    Removes every handler currently registered - this covers both the
    default ``stderr`` handler that loguru installs on import (id 0)
    and any handler installed by a previous call to this function -
    then installs exactly one handler writing to ``stderr``.

    ``backtrace=False`` and ``diagnose=False`` are required: loguru
    defaults both to ``True``, which makes ``logger.exception`` print
    an extended traceback together with the values of local variables
    in every frame. Those locals can hold secrets (a ``Settings``
    instance, a bot token argument, ...), so leaving the defaults on
    turns routine exception logging into a secret-leak channel.

    Removing all handlers before adding a new one makes this function
    idempotent by construction: calling it any number of times leaves
    the process with exactly one handler, without relying on a module
    level flag to distinguish the first call from later ones.
    """
    secrets = tuple(value for value in redact if value)

    def sink(message: str) -> None:
        text = str(message)
        for secret in secrets:
            text = text.replace(secret, _REDACTED)
        sys.stderr.write(text)

    logger.remove()
    logger.add(
        sink,
        colorize=False,
        level=_LOG_LEVEL,
        format=_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
    )
    _install_interceptors()
