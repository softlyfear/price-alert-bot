"""Factories for the Telegram bot -- no module-level state.

Everything that used to live as module attributes (``settings``, ``bot``,
``dp``) is now built by explicit factory functions called from
``app.main``'s ``lifespan``. Importing this module performs no I/O and
creates nothing (PROJECT.md §8.3, Р9).
"""

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Final

from aiogram import BaseMiddleware
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import TelegramObject
from loguru import logger
from redis.asyncio import Redis

from app.bot import texts
from app.core.config import Settings
from app.handlers import create_root_router

_INFLIGHT_KEY: Final[str] = "inflight_updates_tracker"

# ``redis`` 7.4.0 leaves both timeouts as ``None`` by default, so an
# unreachable Redis would otherwise hang bot startup and every FSM operation
# indefinitely instead of failing fast.
_REDIS_CONNECT_TIMEOUT: Final[float] = 5.0
_REDIS_SOCKET_TIMEOUT: Final[float] = 5.0


def create_bot(settings: Settings) -> Bot:
    """Build a ``Bot`` instance with the token passed as a plain string.

    The token leaves ``SecretStr`` exactly once, right here, as a plain
    argument to the ``Bot`` constructor -- it is never wrapped back up or
    copied into another attribute (соглашение 16).
    """
    return Bot(token=settings.tg.BOT_TOKEN.get_secret_value())


def create_storage(settings: Settings) -> RedisStorage:
    """Build ``RedisStorage`` over a ``redis`` client from discrete fields.

    ``RedisSettings.REDIS_URL`` is deliberately not used: it is a plain
    ``str`` carrying the password in clear text end to end (соглашение 16).
    Building the client from ``host``/``port``/``db``/``password`` keeps the
    password out of ``SecretStr`` for exactly one step -- the ``password``
    keyword argument below -- instead of round-tripping it through a URL
    string that would also have to be kept out of logs.
    """
    redis_settings = settings.redis
    password = (
        redis_settings.PASSWORD.get_secret_value()
        if redis_settings.PASSWORD is not None
        else None
    )
    client = Redis(
        host=redis_settings.HOST,
        port=redis_settings.PORT,
        db=redis_settings.DB,
        password=password,
        socket_connect_timeout=_REDIS_CONNECT_TIMEOUT,
        socket_timeout=_REDIS_SOCKET_TIMEOUT,
    )
    return RedisStorage(redis=client)


class InFlightUpdatesTracker(BaseMiddleware):
    """Outer middleware counting Telegram updates currently being handled.

    ``aiogram`` dispatches updates as background tasks
    (``handle_as_tasks=True`` is the default) and does not wait for them
    when polling stops (PROJECT.md §8.3, Р6 (г)). This tracker and
    ``wait_for_inflight_updates`` close that gap so shutdown can wait for
    handlers already running to finish before the ``Bot`` session closes.
    """

    def __init__(self) -> None:
        self._count = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Increment the in-flight count for the duration of the handler call."""
        self._count += 1
        self._idle.clear()
        try:
            return await handler(event, data)
        finally:
            self._count -= 1
            if self._count == 0:
                self._idle.set()

    async def wait_idle(self, timeout: float) -> bool:  # noqa: ASYNC109 -- caller owns the timeout budget for shutdown, not this coroutine
        """Wait until no update is in flight, or until ``timeout`` elapses."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True


def create_dispatcher(storage: BaseStorage) -> Dispatcher:
    """Build the ``Dispatcher``: routers included, in-flight updates tracked."""
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(create_root_router())
    tracker = InFlightUpdatesTracker()
    dispatcher.update.outer_middleware(tracker)
    dispatcher[_INFLIGHT_KEY] = tracker
    return dispatcher


async def wait_for_inflight_updates(dispatcher: Dispatcher, timeout: float) -> bool:  # noqa: ASYNC109
    """Wait for updates already being handled to finish, or until ``timeout``.

    Returns ``True`` if every in-flight update finished within the timeout,
    ``False`` if the timeout elapsed first (PROJECT.md §8.3, Р6 (г)).
    """
    tracker: InFlightUpdatesTracker = dispatcher[_INFLIGHT_KEY]
    return await tracker.wait_idle(timeout)


async def configure_bot_profile(bot: Bot) -> None:
    """Set the bot's description, short description and command menu.

    The three Bot API calls are independent (Р7): a failure in one is
    logged as a warning and does not stop the others or the startup
    sequence that called this function.
    """
    try:
        await bot.set_my_description(description=texts.BOT_DESCRIPTION)
    except Exception as exc:
        logger.warning("Failed to set bot description: {exc}", exc=exc)

    try:
        await bot.set_my_short_description(
            short_description=texts.BOT_SHORT_DESCRIPTION
        )
    except Exception as exc:
        logger.warning("Failed to set bot short description: {exc}", exc=exc)

    try:
        await bot.set_my_commands(commands=texts.BOT_COMMANDS)
    except Exception as exc:
        logger.warning("Failed to set bot commands: {exc}", exc=exc)
