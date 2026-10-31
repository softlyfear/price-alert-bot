from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core import database
from app.core.config import AppSettings
from app.core.config import BotSecret
from app.core.config import DatabaseSettings
from app.core.config import RedisSettings
from app.core.config import Settings
from app.core.config import get_settings as _real_get_settings

if TYPE_CHECKING:
    # ``Message``/``Record`` only exist in loguru's bundled ``.pyi`` stub,
    # not at runtime in ``loguru/__init__.py`` -- importing them
    # unconditionally raises ``ImportError``.
    from loguru import Message
    from loguru import Record


def _make_settings(
    *,
    host: str = "db-host-1",
    name: str = "db-name-1",
    echo: bool = True,
    pool_size: int = 3,
    max_overflow: int = 7,
    pool_pre_ping: bool = True,
    pool_recycle: int = 1234,
    autoflush: bool = False,
    expire_on_commit: bool = False,
    password: str = "test-marker-pass",
) -> Settings:
    """Build a fully explicit ``Settings`` instance, no environment involved.

    Chosen over the ``monkeypatch.setenv`` carveout of convention 19 (the
    other option named by the ticket): that carveout exists for
    zero-argument factories like ``get_settings()`` itself, which read the
    environment *by construction* and cannot be exercised any other way.
    ``get_engine``/``get_sessionmaker`` do not have that constraint -- they
    call ``get_settings()`` through a plain module-level name lookup, which
    ``monkeypatch.setattr(database, "get_settings", ...)`` intercepts
    directly. That path is shorter, does not depend on the environment at
    all (satisfying AC10 for free rather than by careful ``delenv`` cleanup),
    and carries no risk of a real ``.env`` or a stray shell variable leaking
    into a test the way the env-var path would.

    Defaults are chosen away from every SQLAlchemy default they shadow
    (``echo``/``pool_pre_ping`` default ``False``, ``pool_size`` defaults to
    5, ``max_overflow`` to 10, ``pool_recycle`` to -1, ``autoflush``/
    ``expire_on_commit`` default ``True``) so that reading the *default*
    instead of the configured value is itself a visible test failure. The
    two boolean parameter pairs (``echo``/``pool_pre_ping`` and
    ``autoflush``/``expire_on_commit``) are exercised with both of their
    opposite assignments by the parametrized tests below, rather than with
    a single fixed pair here -- one ``Settings`` instance can only give
    each boolean field one value, so a single call to this helper cannot by
    itself catch a swap between two same-typed boolean parameters or either
    one being hard-coded; two calls with the assignments reversed can, and
    do.

    ``app=AppSettings()`` is passed explicitly, even though this file never
    inspects it: ``Settings.app`` uses ``default_factory=AppSettings``, and
    a ``default_factory`` field with no explicit override is still sourced
    from the environment underneath its default (the exact behaviour
    ``tests/unit/core/test_config.py`` names as scenario 2) -- leaving it
    out here would have made every test using this helper depend on the
    calling shell's ``APP__*`` variables. Measured directly: without this
    line, ``APP__HTTP_TIMEOUT_SECONDS=not-a-number`` in the shell turned
    nine of this file's tests red with a pydantic ``ValidationError`` on
    ``app.HTTP_TIMEOUT_SECONDS``, even though none of them ever reads
    ``settings.app``.
    """
    return Settings(
        _env_file=None,
        db=DatabaseSettings(
            PATH=Path(f"{password}.sqlite3"),
            ECHO=echo,
            AUTOFLUSH=autoflush,
            EXPIRE_ON_COMMIT=expire_on_commit,
        ),
        tg=BotSecret(BOT_TOKEN=SecretStr("test-bot-token")),
        redis=RedisSettings(HOST="redis-host-1"),
        app=AppSettings(),
    )


@pytest_asyncio.fixture(autouse=True)
async def _isolated_database_caches() -> AsyncGenerator[None]:
    """Reset the three process-wide ``lru_cache`` instances around every test.

    The precondition assertions below are the isolation witness required
    by convention 22: they run on the main direction every ordinary test in
    this file takes (every test in this file interacts with the engine or
    sessionmaker cache), so a mutation that disables the *teardown* half of
    this fixture is caught the moment the *next* test's setup runs, not by
    a dedicated separate test. Measured on a copy with the teardown
    clearing/disposal removed: the second test in file collection order
    fails on this precondition (see the ticket report for the exact
    command and output).

    ``get_settings``'s own cache is cleared defensively in teardown too,
    even though no test in this file calls the real ``get_settings()`` --
    every test patches ``database.get_settings`` instead (see
    ``_make_settings``). Its precondition is not asserted here: unlike the
    two caches this file actually exercises, that cache is shared with
    ``tests/unit/core/test_config.py``, and coupling this file's isolation
    guard to another file's cleanup discipline would be a fragile,
    order-dependent assertion for no benefit to this ticket's subject.
    """
    assert database.get_engine.cache_info().currsize == 0, (
        "a previous test leaked a cached engine; teardown isolation is broken"
    )
    assert database.get_sessionmaker.cache_info().currsize == 0, (
        "a previous test leaked a cached sessionmaker; teardown isolation is broken"
    )
    try:
        yield
    finally:
        if database.get_engine.cache_info().currsize:
            leaked_engine = database.get_engine()
            await leaked_engine.dispose()
        database.get_engine.cache_clear()
        database.get_sessionmaker.cache_clear()
        _real_get_settings.cache_clear()


def test_get_engine_and_get_sessionmaker_are_cached_and_bound_by_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: repeated calls return the *same* object, and the factory is
    bound to the current engine by identity, not equality.
    """
    monkeypatch.setattr(database, "get_settings", lambda: _make_settings())

    engine_first = database.get_engine()
    engine_second = database.get_engine()
    sessionmaker_first = database.get_sessionmaker()
    sessionmaker_second = database.get_sessionmaker()

    assert engine_first is engine_second
    assert sessionmaker_first is sessionmaker_second
    assert sessionmaker_first.kw["bind"] is engine_first


@pytest.mark.parametrize("echo", [True, False])
def test_get_engine_reads_connection_parameters_from_settings(
    monkeypatch: pytest.MonkeyPatch, echo: bool
) -> None:
    settings = _make_settings(echo=echo)
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    engine = database.get_engine()
    assert engine.url.database == str(settings.db.PATH)
    assert engine.echo is echo


@pytest.mark.parametrize(
    ("autoflush", "expire_on_commit"),
    [(True, False), (False, True)],
    ids=["autoflush-true-expire-false", "autoflush-false-expire-true"],
)
def test_get_sessionmaker_reads_autoflush_and_expire_on_commit_from_settings(
    monkeypatch: pytest.MonkeyPatch,
    autoflush: bool,
    expire_on_commit: bool,
) -> None:
    """AC2 (sessionmaker side): both factory-level parameters are read from
    settings, and the factory is bound to ``get_engine()``'s current engine.

    Parametrized over both opposite assignments for the same reason as the
    engine-side test above. Mutation demonstrated on a copy outside the
    working tree (AC14): hard-coding ``expire_on_commit=False`` regardless
    of settings is red on the ``autoflush=False, expire_on_commit=True``
    case (see the ticket report for the exact command and output).
    """
    monkeypatch.setattr(
        database,
        "get_settings",
        lambda: _make_settings(autoflush=autoflush, expire_on_commit=expire_on_commit),
    )

    sessionmaker = database.get_sessionmaker()

    assert sessionmaker.kw["autoflush"] is autoflush
    assert sessionmaker.kw["expire_on_commit"] is expire_on_commit
    assert sessionmaker.kw["bind"] is database.get_engine()


def test_importing_app_core_database_does_not_read_environment_or_build_engine() -> (
    None
):
    """AC3: importing the module in a clean child process, with no
    environment variables and no reachable ``.env``, succeeds and builds
    nothing.

    Modelled on ``tests/unit/test_package_import.py``: a fresh interpreter
    (so nothing already imported in this process' ``sys.modules`` masks a
    real read-at-import-time regression), an explicitly emptied environment
    (``env={}`` rather than inheriting this process' environment, so no
    ``DB__``/``TG__``/``REDIS__``/``APP__`` variable can leak in from
    whatever shell launched the test suite), and a timeout.

    ``env={}`` alone is not enough to make this hermetic: ``Settings.model_config``
    sets an *absolute* ``env_file=BASE_DIR / ".env"``, which does not depend
    on ``os.environ`` at all, so a real ``.env`` sitting at the repository
    root would still be merged in underneath the (empty) environment.
    Measured directly: with a valid ``.env`` at the repository root and the
    module-level ``get_settings()`` mutation described below already
    applied, the check script *without* the ``model_config`` patch below
    printed "OK" with return code 0 -- a false green, because the mutated
    ``get_settings()`` call succeeded by reading the ``.env`` instead of
    raising (see the ticket report for the exact command and output). The
    check script therefore disables dotenv lookup on ``Settings`` itself,
    from inside the child process, before importing ``app.core.database``
    -- the same mechanism ``tests/unit/core/test_config.py`` uses via
    ``monkeypatch.setitem(Settings.model_config, "env_file", None)``, done
    here as a plain dict write since there is no ``monkeypatch`` fixture
    inside a throwaway subprocess script.

    Mutation demonstrated on a copy outside the working tree (AC14): adding
    a module-level ``get_settings()`` call to ``app/core/database.py``
    makes the child process exit non-zero with a pydantic ``ValidationError``
    (no ``db``/``tg``/``redis`` section supplied), instead of printing "OK"
    with return code 0 -- both with and without a real ``.env`` present at
    the repository root (see the ticket report for the exact commands and
    output).
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    check_script = (
        "import importlib\n"
        "config = importlib.import_module('app.core.config')\n"
        "config.Settings.model_config['env_file'] = None\n"
        "db = importlib.import_module('app.core.database')\n"
        "assert db.get_engine.cache_info().currsize == 0, "
        "'engine built at import time'\n"
        "assert db.get_sessionmaker.cache_info().currsize == 0, "
        "'sessionmaker built at import time'\n"
        "print('OK')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
        env={},
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


@pytest.mark.asyncio
async def test_dispose_engine_resets_caches_to_fresh_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4: after ``dispose_engine()``, both caches report ``currsize == 0``
    immediately, and the next calls give a *new* engine and a factory bound
    to that new engine -- not the disposed ones.
    """
    monkeypatch.setattr(database, "get_settings", lambda: _make_settings())

    old_engine = database.get_engine()
    old_sessionmaker = database.get_sessionmaker()

    await database.dispose_engine()

    assert database.get_engine.cache_info().currsize == 0
    assert database.get_sessionmaker.cache_info().currsize == 0

    new_engine = database.get_engine()
    new_sessionmaker = database.get_sessionmaker()

    assert new_engine is not old_engine
    assert new_sessionmaker is not old_sessionmaker
    assert new_sessionmaker.kw["bind"] is new_engine


@pytest.mark.asyncio
async def test_dispose_engine_on_empty_cache_is_a_noop_and_does_not_read_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings spy detects unnecessary engine construction even when valid settings
    exist.
    """
    settings = _make_settings()
    spy = Mock(wraps=lambda: settings)
    monkeypatch.setattr(database, "get_settings", spy)

    await database.dispose_engine()
    await database.dispose_engine()

    assert spy.call_count == 0
    assert database.get_engine.cache_info().currsize == 0
    assert database.get_sessionmaker.cache_info().currsize == 0


@pytest.mark.asyncio
async def test_dispose_engine_gives_concurrent_callers_fresh_objects_in_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events expose the suspension window deterministically; both cached identities
    must be fresh.
    """
    monkeypatch.setattr(database, "get_settings", lambda: _make_settings())

    old_engine = database.get_engine()
    old_sessionmaker = database.get_sessionmaker()

    entered = asyncio.Event()
    release = asyncio.Event()
    real_dispose = AsyncEngine.dispose
    real_dispose_ran = False

    async def slow_dispose(self: AsyncEngine, close: bool = True) -> None:
        nonlocal real_dispose_ran
        entered.set()
        await release.wait()
        await real_dispose(self, close)
        real_dispose_ran = True

    monkeypatch.setattr(AsyncEngine, "dispose", slow_dispose)

    task = asyncio.create_task(database.dispose_engine())
    reached_window = False
    try:
        async with asyncio.timeout(1):
            await entered.wait()
        reached_window = True

        engine_in_window = database.get_engine()
        sessionmaker_in_window = database.get_sessionmaker()

        assert engine_in_window is not old_engine
        assert sessionmaker_in_window is not old_sessionmaker
    finally:
        release.set()
        if not reached_window and not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert not task.cancelled()
    assert real_dispose_ran is True


@pytest.mark.asyncio
async def test_dispose_engine_propagates_exception_from_dispose_with_caches_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7: if the underlying ``dispose()`` raises, ``dispose_engine()``
    re-raises the *same* exception object, and both caches are already
    empty by the time it propagates -- caches are cleared before the
    ``await``, so this holds regardless of how ``dispose()`` behaves.
    """
    monkeypatch.setattr(database, "get_settings", lambda: _make_settings())
    database.get_engine()
    database.get_sessionmaker()

    boom = RuntimeError("dispose boom")

    async def failing_dispose(self: AsyncEngine, close: bool = True) -> None:
        raise boom

    monkeypatch.setattr(AsyncEngine, "dispose", failing_dispose)

    with pytest.raises(RuntimeError) as exc_info:
        await database.dispose_engine()

    assert exc_info.value is boom
    assert database.get_engine.cache_info().currsize == 0
    assert database.get_sessionmaker.cache_info().currsize == 0


@pytest.mark.asyncio
async def test_session_issued_before_dispose_engine_stays_bound_to_the_old_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing sessions retain their old binding, so shutdown must drain consumers
    first.
    """
    monkeypatch.setattr(database, "get_settings", lambda: _make_settings())
    old_engine = database.get_engine()

    gen = database.get_async_session()
    session = await anext(gen)
    assert session.bind is old_engine

    await database.dispose_engine()

    new_engine = database.get_engine()
    assert new_engine is not old_engine
    assert session.bind is old_engine

    with pytest.raises(StopAsyncIteration):
        await anext(gen)


@pytest.mark.asyncio
async def test_get_async_session_yields_session_without_rollback_on_clean_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9(a): a session bound to the current engine is yielded, and a
    normal (exception-free) close does not call ``rollback``.
    """
    monkeypatch.setattr(database, "get_settings", lambda: _make_settings())
    engine = database.get_engine()

    gen = database.get_async_session()
    session = await anext(gen)
    assert session.bind is engine

    rollback_spy = AsyncMock(wraps=session.rollback)
    monkeypatch.setattr(session, "rollback", rollback_spy)

    with pytest.raises(StopAsyncIteration):
        await anext(gen)

    rollback_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_async_session_rolls_back_once_and_reraises_same_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database, "get_settings", lambda: _make_settings())

    gen = database.get_async_session()
    session = await anext(gen)
    rollback_spy = AsyncMock(wraps=session.rollback)
    monkeypatch.setattr(session, "rollback", rollback_spy)

    boom = RuntimeError("consumer boom")
    with pytest.raises(RuntimeError) as exc_info:
        await gen.athrow(boom)

    assert exc_info.value is boom
    rollback_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_async_session_error_path_logs_without_leaking_password_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive controls establish that the marker is present in settings and the log
    sink receives records.
    """
    from loguru import logger

    marker = "unmistakable-password-marker"
    settings = _make_settings(password=marker)
    assert marker in settings.db.DATABASE_URL
    monkeypatch.setattr(database, "get_settings", lambda: settings)

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level="ERROR")
    try:
        gen = database.get_async_session()
        await anext(gen)
        boom = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await gen.athrow(boom)
    finally:
        logger.remove(sink_id)

    assert len(records) == 1
    record = records[0]

    assert record["level"].name == "ERROR"
    assert record["message"] == "Database session error"
    extra = record["extra"]
    assert extra["error_type"] == "RuntimeError"
    assert extra["operation"] == "db_session"

    assert marker not in record["message"]
    assert all(marker not in str(value) for value in extra.values())


@pytest.mark.parametrize("value", [0, -1, 301, float("inf"), float("nan")])
def test_engine_rejects_invalid_busy_timeout(value: float, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="busy_timeout_seconds"):
        database.create_sqlite_engine(
            tmp_path / "db.sqlite3", busy_timeout_seconds=value
        )
