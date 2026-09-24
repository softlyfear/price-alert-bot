"""Tests for app.main: lifespan, /health, shutdown ordering (PAB-067).

Covers AC5 (honest startup failure), AC6 (/health), AC7 (polling failure
visibility), AC8 (shutdown order) and AC9 (import isolation).

Two different levels of control are used on purpose:

- The AC8 step-ordering/failure-injection tests call ``main._shutdown()``
  directly, against small hand-built doubles for ``Dispatcher``/``Bot``/
  ``RedisStorage`` -- the six steps it orchestrates are already fully
  determined by its own four parameters, so going through the whole
  ``lifespan()`` startup sequence to reach them would only add timing noise
  without exercising anything ``_shutdown`` itself does not already own.
- Tests that need genuine startup behaviour (AC5, AC6, AC7, the AC4(c) 401
  scenario, AC8(c)/(d)) drive the real ``lifespan(app)`` async context
  manager directly (not the ASGI protocol -- see the note on
  ``test_health_...`` below) with every I/O-touching factory
  (``get_settings``, ``get_engine``, ``create_storage``, ``create_bot``,
  ``create_dispatcher``) monkeypatched to fakes from this file and
  ``tests/unit/conftest.py``; the real routing tree
  (``create_root_router()``) is deliberately not involved, since
  ``app.main`` does not own routing correctness (covered by
  ``tests/unit/bot/test_setup.py`` and ``tests/unit/handlers/test_common.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import httpx
import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram import Router
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.methods import SetMyCommands
from aiogram.methods import SetMyDescription
from aiogram.methods import SetMyShortDescription
from aiogram.methods import TelegramMethod
from aiogram.types import Message as TgMessage
from aiogram.types import Update
from pydantic import SecretStr
from pydantic import ValidationError

from app import main
from app.bot.setup import _INFLIGHT_KEY
from app.bot.setup import InFlightUpdatesTracker
from app.bot.setup import create_dispatcher
from app.bot.setup import wait_for_inflight_updates
from app.core.config import AppSettings
from app.core.config import BotSecret
from app.core.config import DatabaseSettings
from app.core.config import RedisSettings
from app.core.config import Settings
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_command_update

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SETTINGS_PREFIX_MODELS = (
    ("DB__", DatabaseSettings),
    ("TG__", BotSecret),
    ("REDIS__", RedisSettings),
    ("APP__", AppSettings),
)


def _delenv_settings_prefix_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every ``DB__``/``TG__``/``REDIS__``/``APP__`` variable
    ``get_settings()`` could read (convention 19), not just the ones a test
    sets itself -- see ``tests/unit/core/test_config.py`` for the measured
    failure mode this guards against.
    """
    for prefix, model in _SETTINGS_PREFIX_MODELS:
        for field_name in model.model_fields:
            monkeypatch.delenv(f"{prefix}{field_name}", raising=False)


# --- AC5: honest startup failure -----------------------------------------


@pytest.mark.asyncio
async def test_lifespan_fails_honestly_on_invalid_db_port_and_missing_bot_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_password_marker = "db-password-marker-XYZ"
    redis_password_marker = "redis-password-marker-XYZ"

    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    main.get_settings.cache_clear()  # type: ignore[attr-defined]

    monkeypatch.setenv("DB__USER", "db_user")
    monkeypatch.setenv("DB__PASSWORD", db_password_marker)
    monkeypatch.setenv("DB__HOST", "db-host")
    monkeypatch.setenv("DB__PORT", "not-a-port")  # invalid: AC5
    monkeypatch.setenv("DB__NAME", "pricebot")
    monkeypatch.setenv("DB__ECHO", "false")
    monkeypatch.setenv("DB__POOL_SIZE", "5")
    monkeypatch.setenv("DB__MAX_OVERFLOW", "10")
    monkeypatch.setenv("DB__POOL_PRE_PING", "true")
    monkeypatch.setenv("DB__POOL_RECYCLE", "1800")
    monkeypatch.setenv("DB__AUTOFLUSH", "false")
    monkeypatch.setenv("DB__EXPIRE_ON_COMMIT", "false")
    # TG__BOT_TOKEN deliberately left unset: AC5
    monkeypatch.setenv("REDIS__HOST", "redis-host")
    monkeypatch.setenv("REDIS__PASSWORD", redis_password_marker)

    # Positive control: the markers really are in the environment this
    # settings construction will read.
    import os

    assert os.environ["DB__PASSWORD"] == db_password_marker
    assert os.environ["REDIS__PASSWORD"] == redis_password_marker

    try:
        with pytest.raises(ValidationError) as exc_info:
            async with main.lifespan(main.app):
                pytest.fail("lifespan must not yield on invalid settings")
    finally:
        main.get_settings.cache_clear()  # type: ignore[attr-defined]

    text = str(exc_info.value)
    assert db_password_marker not in text
    assert redis_password_marker not in text


@pytest.mark.asyncio
async def test_lifespan_fails_honestly_when_redis_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real ``create_storage()`` runs (so the real password-extraction code
    path is exercised); only the network call inside it is intercepted, by
    monkeypatching ``redis.asyncio.Redis.ping`` to fail without opening a
    socket. Also asserts the ping-failure branch closes the storage's
    client exactly once (mutation target: deleting ``await storage.close()``
    inside ``_ensure_redis_available``).
    """
    from redis.asyncio import Redis

    redis_password_marker = "redis-password-marker-unreachable"

    async def failing_ping(self: Redis) -> bool:
        raise ConnectionError("simulated: redis unreachable")

    monkeypatch.setattr(Redis, "ping", failing_ping)

    close_calls: list[None] = []
    real_close = RedisStorage.close

    async def spying_close(self: RedisStorage) -> None:
        close_calls.append(None)
        await real_close(self)

    monkeypatch.setattr(RedisStorage, "close", spying_close)

    settings = _make_orchestration_settings(redis_password=redis_password_marker)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_engine", lambda: None)

    assert settings.redis.PASSWORD is not None
    assert settings.redis.PASSWORD.get_secret_value() == redis_password_marker

    with pytest.raises(RuntimeError, match="Redis is not reachable"):
        async with main.lifespan(main.app):
            pytest.fail("lifespan must not yield when Redis is unreachable")

    err = capsys.readouterr().err
    assert redis_password_marker not in err
    assert len(close_calls) == 1


# --- AC9: import isolation -------------------------------------------------


def test_importing_app_main_in_empty_environment_builds_nothing() -> None:
    """``import app.main`` must succeed with rc=0 and no environment at all:
    settings are read and the ``Bot`` is built only inside ``lifespan()``.
    """
    check_script = (
        "import importlib\n"
        "config = importlib.import_module('app.core.config')\n"
        "config.Settings.model_config['env_file'] = None\n"
        "main = importlib.import_module('app.main')\n"
        "assert hasattr(main, 'app'), 'FastAPI app not built'\n"
        "print('OK')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
        env={},
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# --- Shared doubles for the orchestration-level tests (AC4(c), AC6, AC7) --


class _FakeRedisClient:
    """Stand-in for ``redis.asyncio.Redis``: no socket, ``ping``/``aclose``."""

    def __init__(self, *, ping_raises: Exception | None = None) -> None:
        self._ping_raises = ping_raises
        self.ping_calls = 0
        self.aclose_calls = 0

    async def ping(self) -> bool:
        self.ping_calls += 1
        if self._ping_raises is not None:
            raise self._ping_raises
        return True

    async def aclose(self, close_connection_pool: bool = True) -> None:
        self.aclose_calls += 1


def _make_orchestration_settings(
    *, bot_token: str = "1:orchestration-token", redis_password: str | None = None
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
            HOST="redis-host",
            PASSWORD=SecretStr(redis_password) if redis_password is not None else None,
        ),
        app=AppSettings(),
    )


StartPolling = (
    Any  # Callable[..., Awaitable[None]], kept loose: aiogram's own signature
)


def _controlled_polling() -> tuple[StartPolling, StartPolling]:
    """Build a matched ``(start_polling, stop_polling)`` pair: the former
    blocks until the latter is called, mirroring how the real polling task
    only finishes in response to ``stop_polling`` actually being invoked
    (PROJECT.md §8.3, Р6). Without this pairing, a ``start_polling`` double
    that just hangs forever would deadlock ``_shutdown``'s
    ``asyncio.wait({polling_task})`` step, since nothing would ever make
    the task finish.
    """
    stop_event = asyncio.Event()

    async def start_polling(*args: Any, **kwargs: Any) -> None:
        await stop_event.wait()

    async def stop_polling(*args: Any, **kwargs: Any) -> None:
        stop_event.set()

    return start_polling, stop_polling


def _build_bare_dispatcher(
    storage: RedisStorage,
    *,
    start_polling: StartPolling | None = None,
    stop_polling: StartPolling | None = None,
) -> Dispatcher:
    """Replicate ``create_dispatcher()``'s in-flight-tracker wiring without
    ``create_root_router()`` -- routing correctness is out of scope for the
    orchestration-level tests in this file (see the module docstring). The
    real ``create_dispatcher()`` output, including ``create_root_router()``
    and its middleware wiring, is exercised directly by
    ``test_in_flight_handler_through_the_real_create_dispatcher_is_tracked``
    below and by ``tests/unit/handlers/test_common.py``.

    ``start_polling``, when given, replaces the instance's own
    ``start_polling`` so a test controls exactly when/how the polling task
    finishes, instead of letting the real polling loop spin against the
    fake bot session (which would otherwise busy-loop on ``GetUpdates``
    with no server-side delay to wait on).
    """
    dispatcher = Dispatcher(storage=storage)
    tracker = InFlightUpdatesTracker()
    dispatcher.update.outer_middleware(tracker)
    dispatcher[_INFLIGHT_KEY] = tracker
    if start_polling is not None:
        dispatcher.start_polling = start_polling  # type: ignore[method-assign]
    if stop_polling is not None:
        dispatcher.stop_polling = stop_polling  # type: ignore[method-assign]
    return dispatcher


def _patch_orchestration_factories(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bot: Bot,
    start_polling: StartPolling | None = None,
    stop_polling: StartPolling | None = None,
    get_engine_calls: list[None] | None = None,
    redis_client: _FakeRedisClient | None = None,
) -> RedisStorage:
    settings = _make_orchestration_settings()
    storage = RedisStorage(redis=cast(Any, redis_client or _FakeRedisClient()))
    calls = get_engine_calls if get_engine_calls is not None else []

    def fake_get_engine() -> object:
        calls.append(None)
        return object()

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_engine", fake_get_engine)
    monkeypatch.setattr(main, "create_storage", lambda _settings: storage)
    monkeypatch.setattr(main, "create_bot", lambda _settings: bot)
    monkeypatch.setattr(
        main,
        "create_dispatcher",
        lambda _storage: _build_bare_dispatcher(
            _storage, start_polling=start_polling, stop_polling=stop_polling
        ),
    )
    return storage


# --- AC6: /health ------------------------------------------------------


async def _finish_immediately(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_health_returns_200_while_polling_task_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` reached through the real ASGI route (``httpx.AsyncClient``
    over ``httpx.ASGITransport``), while ``lifespan()`` itself is driven
    directly for deterministic async control over startup/shutdown timing
    (``fastapi.testclient.TestClient`` runs the event loop on a separate
    thread, which would make "is the polling task alive yet" a race).
    """
    get_engine_calls: list[None] = []
    bot = build_fake_bot()
    start_polling, stop_polling = _controlled_polling()
    storage = _patch_orchestration_factories(
        monkeypatch,
        bot=bot,
        start_polling=start_polling,
        stop_polling=stop_polling,
        get_engine_calls=get_engine_calls,
    )

    async with main.lifespan(main.app):
        calls_before_health = len(get_engine_calls)
        redis_client = cast(_FakeRedisClient, storage.redis)
        ping_calls_before = redis_client.ping_calls
        session_requests_before = len(bot.session.requests)  # type: ignore[attr-defined]

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        # AC6: no DB/Redis/network calls made to serve /health itself.
        assert len(get_engine_calls) == calls_before_health
        assert redis_client.ping_calls == ping_calls_before
        assert len(bot.session.requests) == session_requests_before  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_health_returns_503_after_polling_task_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = build_fake_bot()
    _patch_orchestration_factories(
        monkeypatch, bot=bot, start_polling=_finish_immediately
    )

    async with main.lifespan(main.app):
        await main.app.state.polling_task  # deterministic: wait for it to finish

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/health")

        assert response.status_code == 503
        assert response.json() == {"status": "polling stopped"}


# --- AC4(c): a session answering every call with 401 leaks no secret ------


@pytest.mark.asyncio
async def test_startup_against_a_401_bot_session_does_not_leak_the_token_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC4(c): every Bot API call (profile config, then the real polling
    loop's own ``GetMe``) fails with 401. ``preset_me=False`` is required
    for the ``GetMe`` failure to actually happen through the session --
    the default ``preset_me=True`` would skip it entirely.

    Checked on two independent channels (AC4(c) names both): ``loguru``'s
    own handler via ``capsys`` (``setup_logging()`` writes to ``stderr``)
    and the standard-library ``logging`` channel via ``caplog`` --
    pytest's log-capture plugin intercepts standard ``logging`` records
    before they would otherwise reach ``stderr``, so a leak written through
    ``logging.getLogger(...).error(...)`` instead of ``loguru`` stays
    invisible to ``capsys`` alone. Mutation target: adding
    ``logging.getLogger("aiogram").error("t=%s", token)`` anywhere on this
    path. The positive control at the end proves ``caplog`` genuinely
    captures that channel in this environment, so the negative assertion
    on it is not vacuous (convention 22).
    """
    import logging

    from aiogram.exceptions import TelegramUnauthorizedError

    token_marker = "1:unauthorized-token-marker"
    redis_password_marker = "redis-pw-marker-401-test"

    async def unauthorized_responder(method: TelegramMethod[Any]) -> Any:
        raise TelegramUnauthorizedError(method=method, message="Unauthorized")

    bot = build_fake_bot(
        token=token_marker, responder=unauthorized_responder, preset_me=False
    )
    _patch_orchestration_factories(monkeypatch, bot=bot)
    settings = _make_orchestration_settings(
        bot_token=token_marker, redis_password=redis_password_marker
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    assert bot.token == token_marker  # positive control

    with caplog.at_level(logging.DEBUG):
        async with main.lifespan(main.app):
            with pytest.raises(TelegramUnauthorizedError):
                await main.app.state.polling_task

            # Positive control: requests really went out against the fake session.
            assert len(bot.session.requests) > 0  # type: ignore[attr-defined]

    err = capsys.readouterr().err
    assert token_marker not in err
    assert redis_password_marker not in err
    assert token_marker not in caplog.text
    assert redis_password_marker not in caplog.text

    # Positive control: caplog really does capture the standard `logging`
    # channel the assertions above rely on -- without this, an absent
    # marker there would be meaningless if the channel were silently
    # dropped instead of genuinely empty.
    canary_marker = "logging-channel-canary-401-test"
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("aiogram").error("canary: %s", canary_marker)
    assert canary_marker in caplog.text


# --- AC7: polling failure is visible, exactly once, without masking ------


@pytest.mark.asyncio
async def test_polling_task_failure_logs_exactly_one_error_naming_exception_type() -> (
    None
):
    from loguru import logger

    async def boom() -> None:
        raise RuntimeError("polling boom")

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    task = asyncio.create_task(boom())
    task.add_done_callback(main._log_polling_task_failure)

    sink_id = logger.add(_sink, level="ERROR")
    try:
        with pytest.raises(RuntimeError):
            await task
    finally:
        logger.remove(sink_id)

    assert len(records) == 1
    assert records[0]["level"].name == "ERROR"
    assert records[0]["extra"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_polling_task_failure_callback_leaves_no_unretrieved_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``add_done_callback(_log_polling_task_failure)`` retrieves the
    exception via ``task.exception()`` the moment the task finishes, so
    asyncio's own "Task exception was never retrieved" warning (emitted by
    the default exception handler, through the standard ``logging``
    module's ``asyncio`` logger, on garbage collection of a task whose
    exception nobody ever read) must never fire. Observed via ``caplog``,
    not ``capsys``: this warning goes through ``logging``, which pytest's
    log capture intercepts before it would otherwise reach ``stderr``.

    Positive control first: a task whose exception genuinely nobody reads
    at all really does emit that warning in this environment -- without
    it, the assertion below would be vacuously green even if the mechanism
    this test relies on were dead (convention 22).
    """
    import gc
    import logging

    async def boom(marker: str) -> None:
        raise RuntimeError(marker)

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        control_task = asyncio.create_task(boom("unretrieved-control-marker"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert control_task.done()
        del control_task
        gc.collect()
        assert any(
            "Task exception was never retrieved" in r.getMessage()
            for r in caplog.records
        )
        assert any(
            "unretrieved-control-marker" in r.getMessage() for r in caplog.records
        )
        caplog.clear()

        task = asyncio.create_task(boom("retrieved-via-callback-marker"))
        task.add_done_callback(main._log_polling_task_failure)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()
        del task
        gc.collect()

        assert not any(
            "Task exception was never retrieved" in r.getMessage()
            for r in caplog.records
        )
        assert not any(
            "retrieved-via-callback-marker" in r.getMessage() for r in caplog.records
        )


@pytest.mark.asyncio
async def test_log_polling_task_failure_does_not_log_a_cancelled_task() -> None:
    from loguru import logger

    async def wait_forever() -> None:
        await asyncio.Future()

    task = asyncio.create_task(wait_forever())
    await asyncio.sleep(0)
    task.cancel()
    import contextlib

    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level="ERROR")
    try:
        main._log_polling_task_failure(task)
    finally:
        logger.remove(sink_id)

    assert records == []


# --- Boot path wiring: real polling call, start_polling args, profile ----


@pytest.mark.asyncio
async def test_lifespan_registers_the_failure_callback_on_the_real_polling_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7 through the whole ``lifespan``, not just ``_log_polling_task_failure``
    invoked by hand. Mutation target: deleting
    ``polling_task.add_done_callback(_log_polling_task_failure)`` in
    ``lifespan`` -- nothing else in ``lifespan`` retrieves or logs the
    polling task's exception, so this test goes red without that line.

    The sink is added only *after* ``lifespan``'s ``__aenter__()`` runs
    (mirroring ``test_shutdown_does_not_hang_on_a_handler_slower_than_the_timeout``
    below): ``setup_logging()``, the first statement inside ``lifespan``,
    unconditionally removes every existing loguru handler, so a sink added
    beforehand would be silently gone by the time anything is logged.
    """
    from loguru import logger

    async def failing_start_polling(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("polling boom via lifespan")

    bot = build_fake_bot()
    _patch_orchestration_factories(
        monkeypatch, bot=bot, start_polling=failing_start_polling
    )

    lifespan_cm = main.lifespan(main.app)
    await lifespan_cm.__aenter__()  # runs setup_logging(), which clears handlers first

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level="ERROR")
    try:
        with pytest.raises(RuntimeError, match="polling boom via lifespan"):
            await main.app.state.polling_task
    finally:
        logger.remove(sink_id)

    await lifespan_cm.__aexit__(None, None, None)

    error_records = [r for r in records if r["level"].name == "ERROR"]
    assert len(error_records) == 1
    assert error_records[0]["extra"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_lifespan_starts_polling_with_no_signals_and_bot_session_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation target: flipping ``close_bot_session=False`` to ``True`` in
    ``lifespan`` (Р6 (б)) would let aiogram close the ``Bot`` session in
    ``start_polling``'s own ``finally``, out from under handlers still
    running and before ``_shutdown`` gets a chance to; flipping
    ``handle_signals=False`` to aiogram's default would install signal
    handlers that steal SIGINT/SIGTERM from the host process (Р6 (а)).
    """
    calls: list[dict[str, Any]] = []
    stop_event = asyncio.Event()

    async def recording_start_polling(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)
        await stop_event.wait()

    async def stop_polling(*args: Any, **kwargs: Any) -> None:
        stop_event.set()

    bot = build_fake_bot()
    _patch_orchestration_factories(
        monkeypatch,
        bot=bot,
        start_polling=recording_start_polling,
        stop_polling=stop_polling,
    )

    async with main.lifespan(main.app):
        pass

    assert len(calls) == 1
    assert calls[0]["handle_signals"] is False
    assert calls[0]["close_bot_session"] is False


@pytest.mark.asyncio
async def test_lifespan_cleans_up_after_create_bot_fails_post_redis_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation target: deleting the ``except BaseException`` cleanup block
    in ``lifespan`` (storage/engine teardown after a successful Redis check
    but before the polling task exists) would leak the Redis client and
    leave a cached engine behind when e.g. ``create_bot`` raises.
    """

    def failing_create_bot(settings: Settings) -> Bot:
        raise boom

    boom = RuntimeError("create_bot boom")
    dispose_calls: list[None] = []

    async def fake_dispose_engine() -> None:
        dispose_calls.append(None)

    storage = _patch_orchestration_factories(monkeypatch, bot=cast(Bot, object()))
    monkeypatch.setattr(main, "create_bot", failing_create_bot)
    monkeypatch.setattr(main, "dispose_engine", fake_dispose_engine)

    close_calls: list[None] = []
    real_close = storage.close

    async def spying_close() -> None:
        close_calls.append(None)
        await real_close()

    monkeypatch.setattr(storage, "close", spying_close)

    with pytest.raises(RuntimeError) as exc_info:
        async with main.lifespan(main.app):
            pytest.fail("lifespan must not yield when create_bot fails")

    assert exc_info.value is boom
    assert len(close_calls) == 1
    assert len(dispose_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_method",
    [SetMyDescription, SetMyShortDescription, SetMyCommands],
    ids=["description-fails", "short-description-fails", "commands-fail"],
)
async def test_lifespan_starts_polling_after_a_single_profile_call_fails_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing_method: type[TelegramMethod[Any]],
) -> None:
    """AC3's per-call failure tolerance, exercised through the whole
    ``lifespan`` (``configure_bot_profile`` in isolation is already covered
    by ``tests/unit/bot/test_setup.py``): one failing profile call must
    still leave a live polling task behind instead of stopping startup.

    Observed via ``capsys`` (``setup_logging()``'s own ``stderr`` handler),
    not a temporary sink: the warning is logged from inside
    ``configure_bot_profile``, called before ``lifespan`` ever yields
    control back to the caller, so there is no point at which a test-added
    sink could be installed after ``setup_logging()`` resets the handlers
    and before that warning fires.
    """

    async def responder(method: TelegramMethod[Any]) -> Any:
        if isinstance(method, failing_method):
            raise RuntimeError(f"{failing_method.__name__} boom")
        return True

    bot = build_fake_bot(responder=responder)
    start_polling, stop_polling = _controlled_polling()
    _patch_orchestration_factories(
        monkeypatch, bot=bot, start_polling=start_polling, stop_polling=stop_polling
    )

    async with main.lifespan(main.app):
        assert not main.app.state.polling_task.done()

    err = capsys.readouterr().err
    assert err.count("Failed to set bot") == 1
    assert f"{failing_method.__name__} boom" in err


# --- AC8: shutdown order --------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []


class _FakeShutdownBotSession:
    def __init__(self, recorder: _Recorder, *, raises: Exception | None = None) -> None:
        self._recorder = recorder
        self._raises = raises

    async def close(self) -> None:
        self._recorder.events.append("close_bot_session")
        if self._raises is not None:
            raise self._raises


class _FakeShutdownBot:
    def __init__(self, recorder: _Recorder, *, raises: Exception | None = None) -> None:
        self.session = _FakeShutdownBotSession(recorder, raises=raises)


class _FakeShutdownStorage:
    def __init__(self, recorder: _Recorder, *, raises: Exception | None = None) -> None:
        self._recorder = recorder
        self._raises = raises

    async def close(self) -> None:
        self._recorder.events.append("close_storage")
        if self._raises is not None:
            raise self._raises


class _FakeShutdownDispatcher:
    def __init__(
        self,
        recorder: _Recorder,
        *,
        stop_event: asyncio.Event,
        raises: Exception | None = None,
    ) -> None:
        self._recorder = recorder
        self._stop_event = stop_event
        self._raises = raises

    async def stop_polling(self) -> None:
        self._recorder.events.append("stop_polling")
        self._stop_event.set()
        if self._raises is not None:
            raise self._raises


def _build_shutdown_doubles(
    recorder: _Recorder,
    *,
    stop_polling_raises: Exception | None = None,
    close_bot_session_raises: Exception | None = None,
    close_storage_raises: Exception | None = None,
    already_done: bool = False,
) -> tuple[Dispatcher, asyncio.Task[None], Bot, RedisStorage]:
    stop_event = asyncio.Event()

    async def polling_stub() -> None:
        await stop_event.wait()

    polling_task = asyncio.create_task(polling_stub())
    if already_done:
        stop_event.set()

    dispatcher = _FakeShutdownDispatcher(
        recorder, stop_event=stop_event, raises=stop_polling_raises
    )
    bot = _FakeShutdownBot(recorder, raises=close_bot_session_raises)
    storage = _FakeShutdownStorage(recorder, raises=close_storage_raises)
    return (
        cast(Dispatcher, dispatcher),
        polling_task,
        cast(Bot, bot),
        cast(RedisStorage, storage),
    )


def _patch_generic_shutdown_steps(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    *,
    wait_polling_task_raises: Exception | None = None,
    wait_inflight_raises: Exception | None = None,
    dispose_engine_raises: Exception | None = None,
) -> None:
    """Give ``wait_polling_task``/``wait_inflight_updates``/``dispose_engine``
    the same "record an event, optionally fail" shape the three
    object-backed steps get from ``_build_shutdown_doubles`` -- these three
    have no object of their own to hang a double off, since they call a
    bare ``asyncio`` function or a module-level name directly.
    """
    real_wait = asyncio.wait

    async def recording_wait(tasks: Any, **kwargs: Any) -> Any:
        recorder.events.append("wait_polling_task")
        if wait_polling_task_raises is not None:
            raise wait_polling_task_raises
        return await real_wait(tasks, **kwargs)

    monkeypatch.setattr(asyncio, "wait", recording_wait)

    async def fake_wait_for_inflight_updates(
        dispatcher: Any,
        timeout: float,  # noqa: ASYNC109 -- mirrors app.bot.setup.wait_for_inflight_updates
    ) -> bool:
        recorder.events.append("wait_inflight_updates")
        if wait_inflight_raises is not None:
            raise wait_inflight_raises
        return True

    monkeypatch.setattr(
        main, "wait_for_inflight_updates", fake_wait_for_inflight_updates
    )

    async def fake_dispose_engine() -> None:
        recorder.events.append("dispose_engine")
        if dispose_engine_raises is not None:
            raise dispose_engine_raises

    monkeypatch.setattr(main, "dispose_engine", fake_dispose_engine)


_EXPECTED_ORDER = [
    "stop_polling",
    "wait_polling_task",
    "wait_inflight_updates",
    "close_bot_session",
    "close_storage",
    "dispose_engine",
]


@pytest.mark.asyncio
async def test_shutdown_runs_all_six_steps_in_the_exact_documented_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation target: swapping ``close_bot_session``/``dispose_engine``
    order (Р6, the mutation the ticket names explicitly) turns this exact
    sequence comparison red.
    """
    recorder = _Recorder()
    dispatcher, polling_task, bot, storage = _build_shutdown_doubles(recorder)
    _patch_generic_shutdown_steps(monkeypatch, recorder)

    await main._shutdown(dispatcher, polling_task, bot, storage)

    assert recorder.events == _EXPECTED_ORDER


@pytest.mark.asyncio
async def test_shutdown_skips_stop_polling_when_task_already_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    dispatcher, polling_task, bot, storage = _build_shutdown_doubles(
        recorder, already_done=True
    )
    await polling_task  # make "already done" genuinely true before _shutdown runs
    _patch_generic_shutdown_steps(monkeypatch, recorder)

    await main._shutdown(dispatcher, polling_task, bot, storage)

    assert "stop_polling" not in recorder.events
    assert recorder.events == _EXPECTED_ORDER[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_step", _EXPECTED_ORDER)
async def test_shutdown_continues_after_one_step_fails_and_logs_exactly_that_step(
    monkeypatch: pytest.MonkeyPatch, failing_step: str
) -> None:
    from loguru import logger

    recorder = _Recorder()
    boom = RuntimeError(f"{failing_step} boom")

    doubles_kwargs: dict[str, Any] = {}
    generic_kwargs: dict[str, Any] = {}
    if failing_step == "stop_polling":
        doubles_kwargs["stop_polling_raises"] = boom
    elif failing_step == "close_bot_session":
        doubles_kwargs["close_bot_session_raises"] = boom
    elif failing_step == "close_storage":
        doubles_kwargs["close_storage_raises"] = boom
    elif failing_step == "wait_polling_task":
        generic_kwargs["wait_polling_task_raises"] = boom
    elif failing_step == "wait_inflight_updates":
        generic_kwargs["wait_inflight_raises"] = boom
    elif failing_step == "dispose_engine":
        generic_kwargs["dispose_engine_raises"] = boom

    dispatcher, polling_task, bot, storage = _build_shutdown_doubles(
        recorder, **doubles_kwargs
    )
    _patch_generic_shutdown_steps(monkeypatch, recorder, **generic_kwargs)

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level="ERROR")
    try:
        await main._shutdown(dispatcher, polling_task, bot, storage)
    finally:
        logger.remove(sink_id)

    # All six steps still ran, in order, despite the one failure.
    assert recorder.events == _EXPECTED_ORDER

    error_records = [r for r in records if r["level"].name == "ERROR"]
    assert len(error_records) == 1
    assert error_records[0]["extra"]["shutdown_step"] == failing_step
    assert error_records[0]["extra"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_shutdown_does_not_swallow_an_external_cancelled_error() -> None:
    """``_shutdown``'s per-step ``try/except Exception`` must not catch
    ``asyncio.CancelledError`` (a ``BaseException`` subclass since Python
    3.8, not caught by ``except Exception``) -- convention 23. Simulated by
    making one step raise ``CancelledError`` directly and asserting it
    propagates out of ``_shutdown`` instead of being logged and absorbed.
    """
    recorder = _Recorder()
    dispatcher, polling_task, bot, storage = _build_shutdown_doubles(
        recorder, already_done=True
    )
    await polling_task

    async def cancelling_dispose_engine() -> None:
        raise asyncio.CancelledError

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main, "dispose_engine", cancelling_dispose_engine)
        with pytest.raises(asyncio.CancelledError):
            await main._shutdown(dispatcher, polling_task, bot, storage)


# --- AC8(c)/(d): a real in-flight handler, through the whole lifespan ----


def _capture_dispatcher_factory(
    box: list[Dispatcher], *, start_polling: StartPolling, stop_polling: StartPolling
) -> Any:
    def factory(storage: RedisStorage) -> Dispatcher:
        dispatcher = _build_bare_dispatcher(
            storage, start_polling=start_polling, stop_polling=stop_polling
        )
        box.append(dispatcher)
        return dispatcher

    return factory


@pytest.mark.asyncio
async def test_shutdown_waits_for_an_in_flight_handler_before_closing_bot_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8(c): a handler already running when shutdown starts must finish
    -- and its own reply must be observably "sent" -- before the ``Bot``
    session is closed underneath it.
    """
    bot = build_fake_bot()
    start_polling, stop_polling = _controlled_polling()
    _patch_orchestration_factories(
        monkeypatch, bot=bot, start_polling=start_polling, stop_polling=stop_polling
    )
    dispatcher_box: list[Dispatcher] = []
    monkeypatch.setattr(
        main,
        "create_dispatcher",
        _capture_dispatcher_factory(
            dispatcher_box, start_polling=start_polling, stop_polling=stop_polling
        ),
    )

    events: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()
    reached_wait_inflight = asyncio.Event()

    async def slow_handler(event: Any, data: dict[str, Any]) -> None:
        started.set()
        await release.wait()
        events.append("handler_finished")

    real_close = bot.session.close

    async def recording_close() -> None:
        events.append("close_bot_session")
        await real_close()

    monkeypatch.setattr(bot.session, "close", recording_close)

    real_wait_for_inflight_updates = wait_for_inflight_updates

    async def observing_wait_for_inflight_updates(
        dispatcher: Any,
        timeout: float,  # noqa: ASYNC109 -- mirrors app.bot.setup.wait_for_inflight_updates
    ) -> bool:
        reached_wait_inflight.set()
        return await real_wait_for_inflight_updates(dispatcher, timeout)

    monkeypatch.setattr(
        main, "wait_for_inflight_updates", observing_wait_for_inflight_updates
    )

    lifespan_cm = main.lifespan(main.app)
    await lifespan_cm.__aenter__()

    dispatcher = dispatcher_box[0]
    tracker: InFlightUpdatesTracker = dispatcher[_INFLIGHT_KEY]
    handler_task = asyncio.create_task(tracker(slow_handler, Update(update_id=1), {}))
    await started.wait()

    shutdown_task = asyncio.create_task(lifespan_cm.__aexit__(None, None, None))
    async with asyncio.timeout(1):
        await reached_wait_inflight.wait()

    assert not handler_task.done()
    assert bot.session.closed is False  # type: ignore[attr-defined]

    release.set()
    async with asyncio.timeout(1):
        await shutdown_task
    await handler_task

    assert events == ["handler_finished", "close_bot_session"]
    assert bot.session.closed is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_in_flight_handler_through_the_real_create_dispatcher_is_tracked() -> (
    None
):
    """AC8(в) through the real ``app.bot.setup.create_dispatcher`` output,
    not ``_build_bare_dispatcher``. Mutation target: deleting
    ``dispatcher.update.outer_middleware(tracker)`` inside
    ``create_dispatcher`` -- every AC8(в)/(г) test above invokes
    ``InFlightUpdatesTracker`` directly, bypassing the actual middleware
    registration entirely, so that mutation would stay unnoticed by them.
    This test instead routes an update through ``Dispatcher.feed_update``
    on the real factory output, so only a genuinely registered outer
    middleware makes the handler count as in-flight.
    """
    from aiogram.fsm.storage.memory import MemoryStorage

    # ``MemoryStorage``, not ``RedisStorage(redis=_FakeRedisClient())``:
    # a real ``feed_update`` resolves FSM state through the storage before
    # the handler runs, and the ping/aclose-only fake used by the
    # orchestration-level tests elsewhere in this file does not implement
    # the Redis commands that path needs.
    dispatcher = create_dispatcher(MemoryStorage())

    started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    extra_router = Router(name="test-slow-handler")

    @extra_router.message()
    async def slow_handler(message: TgMessage) -> None:
        started.set()
        await release.wait()
        events.append("handler_finished")

    dispatcher.include_router(extra_router)

    bot = build_fake_bot()
    update = make_command_update(
        "not-a-registered-command", chat_id=777_777, user_id=777_777
    )

    handler_task = asyncio.create_task(dispatcher.feed_update(bot, update))
    await started.wait()

    assert await wait_for_inflight_updates(dispatcher, 0.01) is False

    release.set()
    await handler_task

    assert await wait_for_inflight_updates(dispatcher, 1) is True
    assert events == ["handler_finished"]


@pytest.mark.asyncio
async def test_shutdown_does_not_hang_on_a_handler_slower_than_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8(d): a handler that never finishes must not hang shutdown --
    ``_await_inflight_updates`` times out, logs a ``WARNING``, and
    shutdown proceeds to close the bot session regardless.
    """
    from loguru import logger

    monkeypatch.setattr(main, "_INFLIGHT_UPDATES_TIMEOUT_SECONDS", 0.05)

    bot = build_fake_bot()
    start_polling, stop_polling = _controlled_polling()
    _patch_orchestration_factories(
        monkeypatch, bot=bot, start_polling=start_polling, stop_polling=stop_polling
    )
    dispatcher_box: list[Dispatcher] = []
    monkeypatch.setattr(
        main,
        "create_dispatcher",
        _capture_dispatcher_factory(
            dispatcher_box, start_polling=start_polling, stop_polling=stop_polling
        ),
    )

    started = asyncio.Event()
    never_release = asyncio.Event()

    async def hanging_handler(event: Any, data: dict[str, Any]) -> None:
        started.set()
        await never_release.wait()

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    lifespan_cm = main.lifespan(main.app)
    await lifespan_cm.__aenter__()  # runs setup_logging(), which clears handlers first

    handler_task: asyncio.Task[None] | None = None
    sink_id = logger.add(_sink, level="WARNING")
    try:
        dispatcher = dispatcher_box[0]
        tracker: InFlightUpdatesTracker = dispatcher[_INFLIGHT_KEY]
        handler_task = asyncio.create_task(
            tracker(hanging_handler, Update(update_id=1), {})
        )
        await started.wait()

        # AC8(d): shutdown must not hang -- bounded wait proves it.
        async with asyncio.timeout(2):
            await lifespan_cm.__aexit__(None, None, None)
    finally:
        logger.remove(sink_id)
        never_release.set()
        if handler_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await handler_task

    assert bot.session.closed is True  # type: ignore[attr-defined]
    warning_records = [r for r in records if r["level"].name == "WARNING"]
    assert any("Timed out" in r["message"] for r in warning_records)
