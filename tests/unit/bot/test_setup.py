"""Tests for app.bot.setup: factories, secrets, profile, in-flight tracking.

Covers AC3 (profile), AC4(a)/(b) (secrets), and the ``InFlightUpdatesTracker``/
``wait_for_inflight_updates`` machinery app.main's shutdown sequence
(tested end to end in ``tests/unit/test_main.py``) depends on.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Any

import pytest
from aiogram.methods import SetMyCommands
from aiogram.methods import SetMyDescription
from aiogram.methods import SetMyShortDescription
from aiogram.methods import TelegramMethod
from aiogram.types import Update
from pydantic import SecretStr

from app.bot import texts
from app.bot.setup import InFlightUpdatesTracker
from app.bot.setup import configure_bot_profile
from app.bot.setup import create_bot
from app.bot.setup import create_storage
from app.bot.setup import wait_for_inflight_updates
from app.core.config import AppSettings
from app.core.config import BotSecret
from app.core.config import DatabaseSettings
from app.core.config import RedisSettings
from app.core.config import Settings
from app.domain.money import PRICE_DISCLAIMER_FULL
from app.domain.money import PRICE_DISCLAIMER_SHORT
from tests.unit.conftest import build_fake_bot

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record


def _make_settings(
    *,
    bot_token: str = "1:default-marker-token",
    redis_host: str = "redis-host-1",
    redis_port: int = 6379,
    redis_db: int = 0,
    redis_password: str | None = None,
) -> Settings:
    return Settings(
        _env_file=None,
        db=DatabaseSettings(
            USER="u",
            PASSWORD=SecretStr("db-pass"),
            HOST="db-host",
            PORT=5432,
            NAME="db-name",
            ECHO=False,
            POOL_SIZE=1,
            MAX_OVERFLOW=1,
            POOL_PRE_PING=False,
            POOL_RECYCLE=1,
            AUTOFLUSH=False,
            EXPIRE_ON_COMMIT=False,
        ),
        tg=BotSecret(BOT_TOKEN=SecretStr(bot_token)),
        redis=RedisSettings(
            HOST=redis_host,
            PORT=redis_port,
            DB=redis_db,
            PASSWORD=SecretStr(redis_password) if redis_password is not None else None,
        ),
        app=AppSettings(),
    )


# --- AC4(a)/(b): secrets -----------------------------------------------


def test_create_bot_receives_the_token_as_a_plain_string() -> None:
    """Mutation target: passing ``settings.tg.BOT_TOKEN`` (the ``SecretStr``)
    instead of ``.get_secret_value()`` -- aiogram's own ``validate_token``
    rejects a non-``str`` token, so that mutation raises inside
    ``create_bot`` itself instead of merely failing an assertion.
    """
    marker = "1:plain-string-marker"
    settings = _make_settings(bot_token=marker)

    bot = create_bot(settings)

    assert type(bot.token) is str
    assert bot.token == marker


def test_create_bot_leaves_no_token_marker_in_module_level_state() -> None:
    """AC4(b): after import and after ``create_bot``, no attribute of
    ``app.bot.setup`` equals or contains the token marker. Positive
    control: the bot object it just built does carry the marker.
    """
    from app.bot import setup

    marker = "1:leak-detection-marker-XYZ"
    settings = _make_settings(bot_token=marker)

    bot = create_bot(settings)

    for name, value in vars(setup).items():
        assert value != marker, name
        if isinstance(value, str):
            assert marker not in value, name

    assert bot.token == marker


# --- Р4: Redis storage from discrete fields -----------------------------


def test_create_storage_builds_client_from_discrete_host_port_db_password() -> None:
    settings = _make_settings(
        redis_host="redis-42", redis_port=6380, redis_db=3, redis_password="s3cr3t-pass"
    )

    storage = create_storage(settings)

    kwargs = storage.redis.connection_pool.connection_kwargs
    assert kwargs["host"] == "redis-42"
    assert kwargs["port"] == 6380
    assert kwargs["db"] == 3
    assert kwargs["password"] == "s3cr3t-pass"


def test_create_storage_passes_none_password_when_redis_has_no_password() -> None:
    settings = _make_settings(redis_password=None)

    storage = create_storage(settings)

    assert storage.redis.connection_pool.connection_kwargs.get("password") is None


def test_create_storage_sets_connect_and_socket_timeouts_on_the_redis_client() -> None:
    """Mutation target: dropping ``socket_connect_timeout``/``socket_timeout``
    from the ``Redis(...)`` call in ``create_storage`` -- an unreachable
    Redis host would then hang forever instead of failing fast (module
    comment on ``_REDIS_CONNECT_TIMEOUT``/``_REDIS_SOCKET_TIMEOUT``).
    """
    settings = _make_settings()

    storage = create_storage(settings)

    kwargs = storage.redis.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == 5.0
    assert kwargs["socket_timeout"] == 5.0


def test_create_dispatcher_can_be_called_more_than_once_per_process() -> None:
    """Regression guard: ``create_root_router()`` builds its routers via
    factories (``app.handlers.common.create_router()``), not module-level
    ``Router`` singletons -- aiogram raises ``RuntimeError`` the second
    time the same ``Router`` instance is attached to a parent, which a
    singleton router would trigger on the second ``create_dispatcher()``
    call in one process.
    """
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.setup import create_dispatcher

    create_dispatcher(MemoryStorage())
    create_dispatcher(MemoryStorage())  # must not raise


# --- InFlightUpdatesTracker / wait_for_inflight_updates ------------------


@pytest.mark.asyncio
async def test_wait_idle_returns_true_immediately_with_nothing_in_flight() -> None:
    tracker = InFlightUpdatesTracker()
    assert await tracker.wait_idle(0.01) is True


@pytest.mark.asyncio
async def test_wait_idle_returns_false_while_a_handler_is_running_and_true_after() -> (
    None
):
    tracker = InFlightUpdatesTracker()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(event: Any, data: dict[str, Any]) -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(tracker(slow_handler, Update(update_id=1), {}))
    await started.wait()

    assert await tracker.wait_idle(0.01) is False

    release.set()
    await task
    assert await tracker.wait_idle(0.01) is True


@pytest.mark.asyncio
async def test_tracker_reports_idle_after_handler_raises() -> None:
    """Invariant: an exception in the handler still decrements the counter
    (mutation target: dropping the ``finally`` block in
    ``InFlightUpdatesTracker.__call__``, or decrementing only on success).
    """
    tracker = InFlightUpdatesTracker()

    async def boom(event: Any, data: dict[str, Any]) -> None:
        raise RuntimeError("handler boom")

    with pytest.raises(RuntimeError):
        await tracker(boom, Update(update_id=1), {})

    assert await tracker.wait_idle(0.01) is True


@pytest.mark.asyncio
async def test_wait_for_inflight_updates_reflects_the_dispatcher_tracker(
    real_dispatcher: Any,
) -> None:
    from app.bot.setup import _INFLIGHT_KEY

    tracker: InFlightUpdatesTracker = real_dispatcher[_INFLIGHT_KEY]
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(event: Any, data: dict[str, Any]) -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(tracker(slow_handler, Update(update_id=1), {}))
    await started.wait()

    assert await wait_for_inflight_updates(real_dispatcher, 0.01) is False

    release.set()
    assert await wait_for_inflight_updates(real_dispatcher, 1) is True
    await task


# --- AC3: bot profile ----------------------------------------------------


def _requests_of(bot_session: Any) -> list[TelegramMethod[Any]]:
    return list(bot_session.requests)


@pytest.mark.asyncio
async def test_configure_bot_profile_sends_exactly_three_calls() -> None:
    bot = build_fake_bot()

    await configure_bot_profile(bot)

    requests = _requests_of(bot.session)
    descriptions = [r for r in requests if isinstance(r, SetMyDescription)]
    short_descriptions = [r for r in requests if isinstance(r, SetMyShortDescription)]
    commands = [r for r in requests if isinstance(r, SetMyCommands)]

    assert len(requests) == 3
    assert len(descriptions) == 1
    assert len(short_descriptions) == 1
    assert len(commands) == 1

    assert descriptions[0].description == texts.BOT_DESCRIPTION
    assert PRICE_DISCLAIMER_FULL in descriptions[0].description
    assert len(descriptions[0].description) <= 512

    assert short_descriptions[0].short_description == texts.BOT_SHORT_DESCRIPTION
    assert PRICE_DISCLAIMER_SHORT in short_descriptions[0].short_description
    assert len(short_descriptions[0].short_description) <= 120

    assert commands[0].commands == texts.BOT_COMMANDS


async def _run_with_one_failing_call(
    failing_method: type[TelegramMethod[Any]],
) -> tuple[Any, list[Record]]:
    from loguru import logger

    async def responder(method: TelegramMethod[Any]) -> Any:
        if isinstance(method, failing_method):
            raise RuntimeError(f"{failing_method.__name__} boom")
        return True

    bot = build_fake_bot(responder=responder)

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level="WARNING")
    try:
        await configure_bot_profile(bot)
    finally:
        logger.remove(sink_id)

    return bot, records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_method",
    [SetMyDescription, SetMyShortDescription, SetMyCommands],
    ids=["description-fails", "short-description-fails", "commands-fail"],
)
async def test_a_single_failing_profile_call_logs_exactly_one_warning_and_the_rest_run(
    failing_method: type[TelegramMethod[Any]],
) -> None:
    bot, records = await _run_with_one_failing_call(failing_method)

    warning_records = [r for r in records if r["level"].name == "WARNING"]
    assert len(warning_records) == 1

    requests = _requests_of(bot.session)
    assert len(requests) == 3
    assert any(isinstance(r, SetMyDescription) for r in requests)
    assert any(isinstance(r, SetMyShortDescription) for r in requests)
    assert any(isinstance(r, SetMyCommands) for r in requests)
