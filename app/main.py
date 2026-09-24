"""FastAPI application entry point.

Importing this module performs no I/O: it does not read settings, does not
build a ``Bot`` and does not touch the network (PROJECT.md §8.3, Р9). Every
object with a lifetime -- the bot, the dispatcher, the FSM storage, the
polling task -- is created inside ``lifespan`` and torn down there too, so
``uvicorn app.main:app`` stays importable with an empty environment.
"""

import asyncio
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Final
from typing import cast

from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.bot.setup import configure_bot_profile
from app.bot.setup import create_bot
from app.bot.setup import create_dispatcher
from app.bot.setup import create_storage
from app.bot.setup import wait_for_inflight_updates
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.database import get_engine
from app.core.logging import setup_logging

# Budget for handlers already running when shutdown starts to finish before
# the bot session closes under them (PROJECT.md §8.3, Р6 (в)/(г)). No env
# var: this ticket does not add scheduler-grade configuration, only a fixed
# shutdown budget generous enough for one full marketplace HTTP timeout
# (`AppSettings.HTTP_TIMEOUT_SECONDS`, default 10s) plus a database round trip.
_INFLIGHT_UPDATES_TIMEOUT_SECONDS: Final[float] = 30.0


def _log_polling_task_failure(task: "asyncio.Task[None]") -> None:
    """Log a died polling task exactly once (AC7); ignore a clean cancellation.

    Registered via ``add_done_callback`` so the task's exception is retrieved
    as soon as it happens -- whether that is while the app is otherwise idle
    or later, when shutdown itself awaits the task. Retrieving it here also
    keeps asyncio from ever printing "Task exception was never retrieved",
    regardless of which of those two moments comes first.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.bind(error_type=type(exc).__name__).error(
            "Polling task terminated unexpectedly: {exc}", exc=exc
        )


async def _ensure_redis_available(storage: RedisStorage) -> None:
    """Fail fast if Redis is unreachable.

    PROJECT.md §8.3: a silent fallback to ``MemoryStorage`` would lose FSM
    state without anyone noticing, so the app refuses to start instead. The
    just-built client is closed before re-raising so a failed startup does
    not leak the connection; the raised message names no credential, and the
    underlying ``redis`` exception does not echo the configured password
    either -- only host and port ever appear in it.
    """
    try:
        # redis-py's stubs type ``ping()`` as ``Awaitable[bool] | bool``
        # because the same source backs both the sync and async clients;
        # on ``redis.asyncio.Redis`` it is always a coroutine.
        await cast("Awaitable[bool]", storage.redis.ping())
    except Exception as exc:
        await storage.close()
        raise RuntimeError("Redis is not reachable") from exc


async def _stop_polling_if_running(
    dispatcher: Dispatcher, polling_task: "asyncio.Task[None]"
) -> None:
    """Ask aiogram to stop polling, unless the task already finished on its own.

    ``Dispatcher.stop_polling`` raises ``RuntimeError`` when polling is not
    currently running (PROJECT.md §8.3, Р6 (в)); checking ``polling_task``
    first avoids calling it at all in that case instead of relying on the
    exception, so a dead polling task cannot masquerade as a failed shutdown
    step.
    """
    if polling_task.done():
        return
    await dispatcher.stop_polling()


async def _await_polling_task(polling_task: "asyncio.Task[None]") -> None:
    """Wait for the polling task object to finish, without re-raising its result.

    Uses ``asyncio.wait`` instead of a bare ``await`` on purpose: the task's
    own exception, if any, was already retrieved and logged exactly once by
    ``_log_polling_task_failure``. Awaiting the task directly here would
    raise that same exception again and log it a second time.
    """
    await asyncio.wait({polling_task})


async def _await_inflight_updates(dispatcher: Dispatcher) -> None:
    """Wait for updates already being handled, with a bounded timeout.

    A timeout is not an error on its own -- it is logged as a warning
    (PROJECT.md §8.3, Р6 (г)) and shutdown proceeds to close the bot session
    regardless, so a stuck handler cannot hang the whole process.
    """
    finished = await wait_for_inflight_updates(
        dispatcher, _INFLIGHT_UPDATES_TIMEOUT_SECONDS
    )
    if not finished:
        logger.warning(
            "Timed out after {timeout}s waiting for in-flight updates to finish",
            timeout=_INFLIGHT_UPDATES_TIMEOUT_SECONDS,
        )


async def _close_bot_session(bot: Bot) -> None:
    await bot.session.close()


async def _close_storage(storage: RedisStorage) -> None:
    await storage.close()


async def _shutdown(
    dispatcher: Dispatcher,
    polling_task: "asyncio.Task[None]",
    bot: Bot,
    storage: RedisStorage,
) -> None:
    """Tear down everything ``lifespan`` started, in the fixed order (Р6).

    Order: stop polling if it is still running -> wait for the polling task
    -> wait for in-flight updates -> close the bot session -> close the FSM
    storage -> dispose the engine last. A failing step is logged with its
    name and does not cancel the remaining steps, so a partial failure still
    releases as many resources as possible; ``CancelledError`` is never
    caught here and propagates as usual (соглашение 23).
    """
    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("stop_polling", lambda: _stop_polling_if_running(dispatcher, polling_task)),
        ("wait_polling_task", lambda: _await_polling_task(polling_task)),
        ("wait_inflight_updates", lambda: _await_inflight_updates(dispatcher)),
        ("close_bot_session", lambda: _close_bot_session(bot)),
        ("close_storage", lambda: _close_storage(storage)),
        ("dispose_engine", dispose_engine),
    ]
    for name, action in steps:
        try:
            await action()
        except Exception as exc:
            logger.bind(shutdown_step=name, error_type=type(exc).__name__).error(
                "Shutdown step failed: {step}", step=name
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start every dependency, hand control to the app, then tear it down.

    Startup order: ``setup_logging()`` first -> settings -> a single
    ``get_engine()`` call (validates ``DB__*`` without opening a connection)
    -> FSM storage plus a live Redis check -> ``Bot`` and dispatcher ->
    profile -> polling task (PROJECT.md §8.3, Р6 (а)/(б)). The scheduler is
    not started in this ticket (PAB-069).

    A failure after the Redis check (e.g. ``create_bot`` raising
    ``TokenValidationError``) releases the storage and the engine before
    re-raising unchanged -- the ping-failure branch inside
    ``_ensure_redis_available`` already closes the storage itself, so that
    path is left out of this cleanup to avoid closing it twice.
    """
    setup_logging()
    settings = get_settings()
    get_engine()

    storage = create_storage(settings)
    await _ensure_redis_available(storage)

    try:
        bot = create_bot(settings)
        dispatcher = create_dispatcher(storage)
        await configure_bot_profile(bot)

        polling_task: asyncio.Task[None] = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
        )
        polling_task.add_done_callback(_log_polling_task_failure)
        app.state.polling_task = polling_task
    except BaseException:
        await storage.close()
        await dispose_engine()
        raise

    try:
        yield
    finally:
        await _shutdown(dispatcher, polling_task, bot, storage)


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Liveness probe reflecting whether the polling task is still running.

    Deliberately dumb: no database, Redis or network call (PROJECT.md §8.4,
    Р5) -- a dead polling task must stay visible even when everything else
    the process depends on is fine.
    """
    polling_task: asyncio.Task[None] | None = getattr(
        request.app.state, "polling_task", None
    )
    if polling_task is not None and not polling_task.done():
        return JSONResponse({"status": "ok"}, status_code=200)
    return JSONResponse({"status": "polling stopped"}, status_code=503)
