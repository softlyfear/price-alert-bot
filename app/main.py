"""Application resources are created in lifespan; import performs no I/O."""

import asyncio
import functools
import os
import signal
import time
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

from app.bot.keyboards import notice_keyboard
from app.bot.setup import configure_bot_profile
from app.bot.setup import create_bot
from app.bot.setup import create_dispatcher
from app.bot.setup import create_storage
from app.bot.setup import wait_for_inflight_updates
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
from app.services.ozon_access import BrowserOzonAccessProvider
from app.services.ozon_browser import fetch_ozon_access
from app.services.ozon_client import OzonSession
from app.services.price_service import REFRESH_DEADLINE_SECONDS
from app.services.price_service import REFRESH_WINDOW_SECONDS
from app.services.price_service import PriceRefresher
from app.services.price_service import PriceService
from app.services.price_service import RefreshLimiter
from app.services.socks_relay import SocksProxy
from app.services.wb_access import BrowserWbAccessProvider
from app.services.wb_browser import fetch_wb_token

# Shutdown waits are bounded so stuck handlers cannot block resource cleanup.
_INFLIGHT_UPDATES_TIMEOUT_SECONDS: Final[float] = 30.0
# Allow in-progress checks to finish before cancellation; the overall shutdown
# budget must fit the deployment's stop_grace_period.
_SCHEDULER_STOP_TIMEOUT_SECONDS: Final[float] = 45.0
_SCHEDULER_CANCEL_TIMEOUT_SECONDS: Final[float] = 5.0
_WB_ACCESS_CLOSE_TIMEOUT_SECONDS: Final[float] = 10.0
_OZON_ACCESS_CLOSE_TIMEOUT_SECONDS: Final[float] = 10.0


def _log_task_failure(task: "asyncio.Task[None]", label: str) -> None:
    """Retrieve and log the exception once, avoiding an unhandled-task warning."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.bind(error_type=type(exc).__name__).error(
            "{label} task terminated unexpectedly: {exc}", label=label, exc=exc
        )


def _log_polling_task_failure(task: "asyncio.Task[None]") -> None:
    _log_task_failure(task, "Polling")


def _log_scheduler_task_failure(task: "asyncio.Task[None]") -> None:
    _log_task_failure(task, "Scheduler")


def _request_process_termination() -> None:
    """Use uvicorn’s normal shutdown path to terminate the process."""
    os.kill(os.getpid(), signal.SIGTERM)


class _BackgroundDeathWatch:
    """Request termination once if a background task dies before shutdown."""

    def __init__(self) -> None:
        self.stopping = False
        self._requested = False

    def task_done(self, task: "asyncio.Task[None]", label: str) -> None:
        if self.stopping or self._requested:
            return
        self._requested = True
        if task.cancelled():
            outcome = "cancelled"
        elif task.exception() is not None:
            outcome = "failed"
        else:
            outcome = "returned"
        logger.bind(background_task=label, outcome=outcome).critical(
            "Background task died before shutdown; terminating the process"
        )
        _request_process_termination()


async def _ensure_redis_available(storage: RedisStorage) -> None:
    """Reject unreachable Redis rather than silently lose persistent FSM state."""
    try:
        # The shared sync/async stub permits bool; the async client returns a coroutine.
        await cast("Awaitable[bool]", storage.redis.ping())
    except Exception as exc:
        await storage.close()
        raise RuntimeError("Redis is not reachable") from exc


async def _ensure_telegram_available(bot: Bot) -> None:
    """Check Telegram before starting tasks; redact the token from errors and logs."""
    try:
        await bot.me()
    except Exception as exc:
        error_type = type(exc).__name__
        text = str(exc)
        secret = bot.token.partition(":")[2]
        if secret:
            text = text.replace(secret, "***")
        logger.bind(error_type=error_type).error(
            "Telegram is not available at startup: " + text
        )
        raise RuntimeError(
            f"Telegram is not available at startup: {error_type}"
        ) from None


async def _stop_polling_if_running(
    dispatcher: Dispatcher, polling_task: "asyncio.Task[None]"
) -> None:
    """Avoid stop_polling raising when polling has already stopped."""
    if polling_task.done():
        return
    await dispatcher.stop_polling()


async def _await_polling_task(polling_task: "asyncio.Task[None]") -> None:
    """Wait without re-raising an exception already logged by the done callback."""
    await asyncio.wait({polling_task})


async def _await_inflight_updates(dispatcher: Dispatcher) -> None:
    """Bound the wait so a stuck handler cannot block shutdown."""
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
    """Bound the wait and cancellation; task exceptions were already logged."""
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


async def _close_wb_access(wb_access: BrowserWbAccessProvider) -> None:
    await asyncio.wait_for(wb_access.aclose(), _WB_ACCESS_CLOSE_TIMEOUT_SECONDS)


async def _close_ozon_access(ozon_access: BrowserOzonAccessProvider) -> None:
    await asyncio.wait_for(ozon_access.aclose(), _OZON_ACCESS_CLOSE_TIMEOUT_SECONDS)


async def _close_ozon_session(ozon_session: OzonSession) -> None:
    await ozon_session.aclose()


def _serialized[T](
    lock: asyncio.Lock, fetch: Callable[[], Awaitable[T]]
) -> Callable[[], Awaitable[T]]:
    """Serialize browser fetches; cancellation also releases the lock."""

    async def run() -> T:
        async with lock:
            return await fetch()

    return run


async def _close_storage(storage: RedisStorage) -> None:
    await storage.close()


async def _run_steps(steps: list[tuple[str, Callable[[], Awaitable[None]]]]) -> None:
    """Continue after failed teardown steps, while propagating cancellation."""
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
    wb_access: BrowserWbAccessProvider,
    ozon_access: BrowserOzonAccessProvider,
    ozon_session: OzonSession,
) -> None:
    """Stop workers before closing their resources; dispose the engine last."""
    await _run_steps(
        [
            ("signal_scheduler_stop", lambda: _signal_scheduler_stop(stop_event)),
            ("close_wb_access", lambda: _close_wb_access(wb_access)),
            ("close_ozon_access", lambda: _close_ozon_access(ozon_access)),
            (
                "stop_polling",
                lambda: _stop_polling_if_running(dispatcher, polling_task),
            ),
            ("wait_polling_task", lambda: _await_polling_task(polling_task)),
            ("wait_inflight_updates", lambda: _await_inflight_updates(dispatcher)),
            ("wait_scheduler_task", lambda: _await_scheduler_task(scheduler_task)),
            ("close_bot_session", lambda: _close_bot_session(bot)),
            ("close_http_client", lambda: _close_http_client(http_client)),
            ("close_ozon_session", lambda: _close_ozon_session(ozon_session)),
            ("close_storage", lambda: _close_storage(storage)),
            ("dispose_engine", dispose_engine),
        ]
    )


async def _cleanup_failed_startup(
    bot: Bot | None,
    http_client: httpx.AsyncClient | None,
    storage: RedisStorage,
    wb_access: BrowserWbAccessProvider | None = None,
    ozon_access: BrowserOzonAccessProvider | None = None,
    ozon_session: OzonSession | None = None,
) -> None:
    """Release successfully created resources, isolating each cleanup step."""
    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    if wb_access is not None:
        steps.append(("close_wb_access", lambda: _close_wb_access(wb_access)))
    if ozon_access is not None:
        steps.append(("close_ozon_access", lambda: _close_ozon_access(ozon_access)))
    if bot is not None:
        steps.append(("close_bot_session", lambda: _close_bot_session(bot)))
    if http_client is not None:
        steps.append(("close_http_client", lambda: _close_http_client(http_client)))
    if ozon_session is not None:
        steps.append(("close_ozon_session", lambda: _close_ozon_session(ozon_session)))
    steps.append(("close_storage", lambda: _close_storage(storage)))
    steps.append(("dispose_engine", dispose_engine))
    await _run_steps(steps)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Start resources and tasks; clean up on startup failure or shutdown.

    A background task dying before shutdown triggers SIGTERM for a restart.
    Telegram is checked before tasks start, with token redaction enabled.
    """
    setup_logging()
    settings = get_settings()
    get_engine()
    storage = create_storage(settings)
    await _ensure_redis_available(storage)

    watch = _BackgroundDeathWatch()
    bot: Bot | None = None
    http_client: httpx.AsyncClient | None = None
    wb_access: BrowserWbAccessProvider | None = None
    ozon_access: BrowserOzonAccessProvider | None = None
    ozon_session: OzonSession | None = None
    try:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.app.HTTP_TIMEOUT_SECONDS)
        )
        # One browser at a time in the process: both fetchers share one lock.
        browser_lock = asyncio.Lock()
        # Lazy: no browser starts until the first WB price check needs a token.
        wb_access = BrowserWbAccessProvider(
            _serialized(
                browser_lock,
                lambda: fetch_wb_token(sandbox=settings.wb.BROWSER_SANDBOX),
            ),
            token_timeout_seconds=settings.wb.TOKEN_TIMEOUT_SECONDS,
            failure_pause_seconds=settings.wb.FAILURE_PAUSE_SECONDS,
            launch_budget=settings.wb.LAUNCH_BUDGET,
            launch_window_seconds=settings.wb.LAUNCH_WINDOW_SECONDS,
        )
        # Share the proxy between browser and price session for one exit IP.
        proxy_url = settings.ozon.PROXY_URL
        ozon_proxy = (
            SocksProxy.from_url(proxy_url.get_secret_value())
            if proxy_url is not None
            else None
        )
        ozon_session = OzonSession(proxy=ozon_proxy)
        # Lazy as well: Chromium starts on the first Ozon check, not at startup.
        ozon_access = BrowserOzonAccessProvider(
            _serialized(
                browser_lock,
                functools.partial(
                    fetch_ozon_access,
                    sandbox=settings.ozon.BROWSER_SANDBOX,
                    proxy=ozon_proxy,
                ),
            ),
            token_timeout_seconds=settings.ozon.TOKEN_TIMEOUT_SECONDS,
            failure_pause_seconds=settings.ozon.FAILURE_PAUSE_SECONDS,
            launch_budget=settings.ozon.LAUNCH_BUDGET,
            launch_window_seconds=settings.ozon.LAUNCH_WINDOW_SECONDS,
        )
        client_factory = functools.partial(
            get_client,
            wb_access=wb_access,
            ozon_access=ozon_access,
            ozon_session=ozon_session,
        )
        bot = create_bot(settings)
        setup_logging(redact=[bot.token.partition(":")[2]])
        await _ensure_telegram_available(bot)
        sessionmaker = get_sessionmaker()
        dispatcher = create_dispatcher(
            storage,
            sessionmaker,
            http_client,
            settings.app.MAX_PRODUCTS_PER_USER,
            client_factory,
        )
        price_service = PriceService(
            notification_service=NotificationService(
                bot, alert_keyboard=notice_keyboard
            ),
            product_repo_factory=ProductRepository,
            alert_repo_factory=AlertRepository,
            user_repo_factory=UserRepository,
            client_factory=client_factory,
            http_client=http_client,
            alert_cooldown_seconds=settings.app.ALERT_COOLDOWN_SECONDS,
        )
        dispatcher["price_refresher"] = PriceRefresher(
            price_service,
            sessionmaker,
            ProductRepository,
            UserRepository,
            concurrency=settings.app.MARKETPLACE_CONCURRENCY,
            limiter=RefreshLimiter(REFRESH_WINDOW_SECONDS, time.monotonic),
            deadline_seconds=REFRESH_DEADLINE_SECONDS,
        )
        dispatcher["display_timezone"] = settings.app.DISPLAY_TIMEZONE
        await configure_bot_profile(bot)

        polling_task: asyncio.Task[None] = asyncio.create_task(
            dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
        )
        polling_task.add_done_callback(_log_polling_task_failure)
        polling_task.add_done_callback(lambda t: watch.task_done(t, "Polling"))
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
        scheduler_task.add_done_callback(lambda t: watch.task_done(t, "Scheduler"))
        app.state.scheduler_task = scheduler_task
    except BaseException:
        watch.stopping = True
        await _cleanup_failed_startup(
            bot, http_client, storage, wb_access, ozon_access, ozon_session
        )
        raise

    try:
        yield
    finally:
        watch.stopping = True
        await _shutdown(
            dispatcher,
            polling_task,
            scheduler_task,
            stop_event,
            bot,
            http_client,
            storage,
            wb_access,
            ozon_access,
            ozon_session,
        )


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Report background task liveness without database or network probes."""
    for state_name, label in (
        ("polling_task", "polling stopped"),
        ("scheduler_task", "scheduler stopped"),
    ):
        task: asyncio.Task[None] | None = getattr(request.app.state, state_name, None)
        if task is None or task.done():
            return JSONResponse({"status": label}, status_code=503)
    return JSONResponse({"status": "ok"}, status_code=200)
