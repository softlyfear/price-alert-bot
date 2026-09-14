"""Loguru configuration for the application process.

Provides a single entry point that configures the root loguru logger.
Nothing runs at import time; the caller decides when logging becomes
active (see PAB-033 for the intended call site in ``app.main``).
"""

import sys

from loguru import logger

_LOG_LEVEL = "INFO"
_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level> | {extra}"
)


def setup_logging() -> None:
    """Configure the root loguru logger for the process.

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
    logger.remove()
    logger.add(
        sys.stderr,
        level=_LOG_LEVEL,
        format=_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
    )
