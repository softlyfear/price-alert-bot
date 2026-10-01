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

import httpx
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
from app.core.config import Settings
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.database import get_engine
from app.core.database import get_sessionmaker
from app.core.logging import setup_logging
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.scheduler import price_check_loop
from app.services.client_factory import get_client
from app.services.notification import NotificationService
from app.services.price_service import PriceService

# Budget for handlers already running when shutdown starts to finish before
# the bot session closes under them (PROJECT.md §8.3, Р6 (в)/(г)). No env
# var: this ticket does not add scheduler-grade configuration, only a fixed
# shutdown budget generous enough for one full marketplace HTTP timeout
# (`AppSettings.HTTP_TIMEOUT_SECONDS`, default 10s) plus a database round trip.
_INFLIGHT_UPDATES_TIMEOUT_SECONDS: Final[float] = 30.0

# Budget for the scheduler to finish the products already in progress after
# ``stop_event`` is set (new products are not started). One product costs at
# most one marketplace deadline (`WBClient.TIMEOUT_SECONDS`, 10s) plus a
# Telegram send (aiogram's default request timeout is 60s, but a healthy send
# takes well under a second) plus database round trips; 45s covers the fetch,
# a slow send and the commit. It runs concurrently with the polling stop and
# the in-flight wait, so the worst-case shutdown is roughly
# 30 + 45 + _SCHEDULER_CANCEL_TIMEOUT_SECONDS plus the closing steps -- the
# figure PAB-036 must keep below ``stop_grace_period``.
_SCHEDULER_STOP_TIMEOUT_SECONDS: Final[float] = 45.0
# How long to wait for a scheduler task to actually finish after ``cancel()``.
_SCHEDULER_CANCEL_TIMEOUT_SECONDS: Final[float] = 5.0


def _log_task_failure(task: "asyncio.Task[None]", label: str) -> None:
    """Log a died background task exactly once; ignore a clean cancellation.

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
            "{label} task terminated unexpectedly: {exc}", label=label, exc=exc
        )


def _log_polling_task_failure(task: "asyncio.Task[None]") -> None:
    """Log a died polling task exactly once (AC7)."""
    _log_task_failure(task, "Polling")


def _log_scheduler_task_failure(task: "asyncio.Task[None]") -> None:
    """Log a died scheduler task exactly once (PAB-069, Р3)."""
    _log_task_failure(task, "Scheduler")


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


async def _signal_scheduler_stop(stop_event: asyncio.Event) -> None:
    stop_event.set()


async def _await_scheduler_task(scheduler_task: "asyncio.Task[None]") -> None:
    """Wait for the scheduler to finish; on timeout warn, cancel and wait briefly.

    Like ``_await_polling_task`` this never re-raises the task's own exception:
    ``_log_scheduler_task_failure`` already logged it once.
    """
    done, _ = await asyncio.wait(
        {scheduler_task}, timeout=_SCHEDULER_STOP_TIMEOUT_SECONDS
    )
    if done:
        return
    logger.warning(
        "Timed out after {timeout}s waiting for the scheduler to stop; cancelling",
        timeout=_SCHEDULER_STOP_TIMEOUT_SECONDS,
    )
    scheduler_task.cancel()
    done, _ = await asyncio.wait(
        {scheduler_task}, timeout=_SCHEDULER_CANCEL_TIMEOUT_SECONDS
    )
    if not done:
        logger.warning("Scheduler task did not finish after cancellation")


async def _close_bot_session(bot: Bot) -> None:
    await bot.session.close()


async def _close_http_client(http_client: httpx.AsyncClient) -> None:
    await http_client.aclose()


async def _close_storage(storage: RedisStorage) -> None:
    await storage.close()


async def _run_steps(steps: list[tuple[str, Callable[[], Awaitable[None]]]]) -> None:
    """Run teardown steps in order; a failing step is logged, the rest still run.

    ``CancelledError`` is never caught here and propagates as usual
    (соглашение 23).
    """
    for name, action in steps:
        try:
            await action()
        except Exception as exc:
            logger.bind(shutdown_step=name, error_type=type(exc).__name__).error(
                "Shutdown step failed: {step}", step=name
            )


async def _shutdown(
    dispatcher: Dispatcher,
    polling_task: "asyncio.Task[None]",
    scheduler_task: "asyncio.Task[None]",
    stop_event: asyncio.Event,
    bot: Bot,
    http_client: httpx.AsyncClient,
    storage: RedisStorage,
) -> None:
    """Tear down everything ``lifespan`` started, in the fixed order (Р6, Р3).

    Order: signal the scheduler to stop -> stop polling if it is still running
    -> wait for the polling task -> wait for in-flight updates -> wait for the
    scheduler (bounded; it uses the bot and the HTTP client) -> close the bot
    session -> close the shared HTTP client -> close the FSM storage -> dispose
    the engine last. A failing step is logged with its name and does not cancel
    the remaining steps, so a partial failure still releases as many resources
    as possible.
    """
    await _run_steps(
        [
            ("signal_scheduler_stop", lambda: _signal_scheduler_stop(stop_event)),
            (
                "stop_polling",
                lambda: _stop_polling_if_running(dispatcher, polling_task),
            ),
            ("wait_polling_task", lambda: _await_polling_task(polling_task)),
            ("wait_inflight_updates", lambda: _await_inflight_updates(dispatcher)),
            ("wait_scheduler_task", lambda: _await_scheduler_task(scheduler_task)),
            ("close_bot_session", lambda: _close_bot_session(bot)),
            ("close_http_client", lambda: _close_http_client(http_client)),
            ("close_storage", lambda: _close_storage(storage)),
            ("dispose_engine", dispose_engine),
        ]
    )


async def _cleanup_failed_startup(
    bot: Bot | None, http_client: httpx.AsyncClient | None, storage: RedisStorage
) -> None:
    """Release whatever startup managed to create, each step isolated.

    ``bot`` and ``http_client`` are ``None`` when their constructor did not
    finish; such a resource is skipped rather than touched.
    """
    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    if bot is not None:
        steps.append(("close_bot_session", lambda: _close_bot_session(bot)))
    if http_client is not None:
        steps.append(("close_http_client", lambda: _close_http_client(http_client)))
    steps.append(("close_storage", lambda: _close_storage(storage)))
    steps.append(("dispose_engine", dispose_engine))
    await _run_steps(steps)


def _warn_if_pool_budget_exceeded(settings: Settings) -> None:
    """Warn when the scheduler may want more connections than the pool offers.

    Each concurrent check holds one pooled connection; the bot's handlers need
    some too. Only a warning: startup proceeds, workers queue for connections.
    """
    capacity = settings.db.POOL_SIZE + settings.db.MAX_OVERFLOW
    if capacity <= settings.app.MARKETPLACE_CONCURRENCY:
        logger.warning(
            "MARKETPLACE_CONCURRENCY={concurrency} is not below the DB pool "
            "capacity POOL_SIZE + MAX_OVERFLOW={capacity}; bot handlers may "
            "wait for connections",
            concurrency=settings.app.MARKETPLACE_CONCURRENCY,
            capacity=capacity,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start every dependency, hand control to the app, then tear it down.

    Startup order: ``setup_logging()`` first -> settings -> a single
    ``get_engine()`` call (validates ``DB__*`` without opening a connection)
    -> FSM storage plus a live Redis check -> the shared ``httpx.AsyncClient``
    (one per process, timeout from ``APP__HTTP_TIMEOUT_SECONDS``) -> ``Bot``
    and dispatcher -> services -> profile -> polling task -> scheduler task
    (PROJECT.md §8.3, Р6 (а)/(б); PAB-069, Р1).

    A failure after the Redis check (e.g. ``create_bot`` raising
    ``TokenValidationError``) releases whatever was created -- bot session,
    HTTP client, storage, engine -- in isolated steps before re-raising
    unchanged. The ping-failure branch inside ``_ensure_redis_available``
    already closes the storage itself, so that path is left out of this
    cleanup to avoid closing it twice.
    """
    setup_logging()
    settings = get_settings()
    get_engine()
    _warn_if_pool_budget_exceeded(settings)

    storage = create_storage(settings)
    await _ensure_redis_available(storage)

    bot: Bot | None = None
    http_client: httpx.AsyncClient | None = None
    try:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.app.HTTP_TIMEOUT_SECONDS)
        )
        bot = create_bot(settings)
        sessionmaker = get_sessionmaker()
        dispatcher = create_dispatcher(
            storage,
            sessionmaker,
            http_client,
            settings.app.MAX_PRODUCTS_PER_USER,
        )
        price_service = PriceService(
            notification_service=NotificationService(bot),
            product_repo_factory=ProductRepository,
            alert_repo_factory=AlertRepository,
            user_repo_factory=UserRepository,
            client_factory=get_client,
            http_client=http_client,
            alert_cooldown_seconds=settings.app.ALERT_COOLDOWN_SECONDS,
        )
        await configure_bot_profile(bot)

        polling_task: asyncio.Task[None] = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
        )
        polling_task.add_done_callback(_log_polling_task_failure)
        app.state.polling_task = polling_task

        stop_event = asyncio.Event()
        scheduler_task: asyncio.Task[None] = asyncio.create_task(
            price_check_loop(
                price_service,
                sessionmaker,
                interval_seconds=settings.app.SCHEDULER_INTERVAL_SECONDS,
                concurrency=settings.app.MARKETPLACE_CONCURRENCY,
                stop_event=stop_event,
            )
        )
        scheduler_task.add_done_callback(_log_scheduler_task_failure)
        app.state.scheduler_task = scheduler_task
    except BaseException:
        await _cleanup_failed_startup(bot, http_client, storage)
        raise

    try:
        yield
    finally:
        await _shutdown(
            dispatcher,
            polling_task,
            scheduler_task,
            stop_event,
            bot,
            http_client,
            storage,
        )


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Liveness probe reflecting both background tasks: polling and scheduler.

    Deliberately dumb: no database, Redis or network call (PROJECT.md §8.4,
    Р4/Р5) -- a dead task must stay visible even when everything else the
    process depends on is fine. The body names the first dead task.
    """
    for state_name, label in (
        ("polling_task", "polling stopped"),
        ("scheduler_task", "scheduler stopped"),
    ):
        task: asyncio.Task[None] | None = getattr(request.app.state, state_name, None)
        if task is None or task.done():
            return JSONResponse({"status": label}, status_code=503)
    return JSONResponse({"status": "ok"}, status_code=200)
