"""Tests for app.core.database: get_engine, get_sessionmaker, dispose_engine,
and get_async_session.

Companion coverage ticket (convention 9) to PAB-005, which built this
module, and to PAB-055, which fixed the ``dispose_engine()`` ordering the
AC6/AC7-shaped tests below exercise directly. After this file,
``app/core/database.py`` must reach 100% line and branch coverage.

No PostgreSQL container is needed anywhere in this file. ``create_async_engine()``
never opens a network connection by itself, and ``AsyncSession.rollback()`` on
a session that never checked out a connection is a safe no-op -- both
verified independently on a throwaway engine pointed at a host that does not
exist, outside the working tree, before this file was written.

Isolation is the main hazard here, not correctness: ``get_settings``,
``get_engine`` and ``get_sessionmaker`` are three ``functools.lru_cache``
instances, i.e. process-global state. The autouse fixture below resets all
three around every test and asserts, before each test runs, that the two
caches this file is about are already empty -- turning "a previous test
leaked a cached engine" into a loud, immediate failure on the very next
test instead of a silent cross-test contamination.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
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
            USER="test-user",
            PASSWORD=SecretStr(password),
            HOST=host,
            PORT=5432,
            NAME=name,
            ECHO=echo,
            POOL_SIZE=pool_size,
            MAX_OVERFLOW=max_overflow,
            POOL_PRE_PING=pool_pre_ping,
            POOL_RECYCLE=pool_recycle,
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


@pytest.mark.parametrize(
    ("echo", "pool_pre_ping"),
    [(True, False), (False, True)],
    ids=["echo-true-preping-false", "echo-false-preping-true"],
)
def test_get_engine_reads_pool_and_connection_parameters_from_settings(
    monkeypatch: pytest.MonkeyPatch,
    echo: bool,
    pool_pre_ping: bool,
) -> None:
    """AC2 (engine side): all six engine-level parameters are read from
    settings at construction time and land on the real engine/pool objects.

    ``echo``/``pool_pre_ping`` are parametrized over *both* opposite
    boolean assignments rather than fixed at one pair: a single ``Settings``
    instance cannot distinguish "read the configured value" from "always
    return ``True``" for a field whose configured value happens to be
    ``True`` in that one instance, and cannot catch the two engine
    parameters being swapped with each other unless they hold different
    values -- running both assignments closes both gaps at once, without
    needing them to differ from *each other* in a single call the way
    ``pool_size``/``max_overflow`` do below.

    Mutations demonstrated on copies outside the working tree (AC14), all
    four red on at least one of the two parametrized cases (see the ticket
    report for the exact commands and output):

    - swapping ``echo=`` and ``pool_pre_ping=`` at the
      ``create_async_engine(...)`` call site;
    - hard-coding ``echo=True`` regardless of settings;
    - hard-coding ``pool_pre_ping=True`` regardless of settings;
    - swapping ``pool_size=``/``max_overflow=`` (distinct integers, so this
      one is caught on either parametrized case).
    """
    monkeypatch.setattr(
        database,
        "get_settings",
        lambda: _make_settings(
            host="params-host",
            name="params-db",
            echo=echo,
            pool_size=3,
            max_overflow=7,
            pool_pre_ping=pool_pre_ping,
            pool_recycle=1234,
        ),
    )

    engine = database.get_engine()

    assert engine.url.host == "params-host"
    assert engine.url.database == "params-db"
    assert engine.echo is echo
    assert engine.pool.size() == 3  # type: ignore[attr-defined]
    assert engine.pool._max_overflow == 7  # type: ignore[attr-defined]
    assert engine.pool._pre_ping is pool_pre_ping
    assert engine.pool._recycle == 1234


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
    """AC5: two consecutive ``dispose_engine()`` calls with nothing built
    yet neither raise nor read settings.

    A spy on ``get_settings`` is used rather than "no exception was
    raised", per the ticket's own warning: with valid settings available,
    an implementation that dropped the ``if get_engine.cache_info().currsize``
    guard entirely would still build, cache and successfully dispose an
    engine on both calls without raising -- "did not raise" would stay
    green on that broken implementation. Call-count is the assertion that
    actually distinguishes the two.

    Mutation demonstrated on a copy outside the working tree (AC14):
    removing the ``if get_engine.cache_info().currsize:`` guard from
    ``dispose_engine`` (so it always calls ``get_engine()``) turns
    ``spy.call_count == 0`` into ``spy.call_count == 2`` (see the ticket
    report for the exact command and output).
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
    """AC6: the key PAB-055 scenario. While ``dispose()`` is suspended, a
    concurrent caller must already see a *new* engine and a *new*
    sessionmaker -- not the ones about to be disposed.

    Deterministic interleaving via ``asyncio.Event``, no ``sleep``:
    ``AsyncEngine.dispose`` is replaced with a coroutine that signals
    ``entered`` the instant it starts and then waits on ``release`` before
    calling through to the real implementation. ``dispose_engine()`` is
    started as a task; once ``entered`` fires, this test is guaranteed to
    observe exactly the state ``dispose_engine()`` leaves behind *before*
    the awaited ``dispose()`` call resumes.

    Checking *both* identities is required, not optional: measured on
    three copies of ``dispose_engine`` outside the working tree (AC14), each
    swapping the order of one clear relative to the ``await`` --

    - (a) both ``cache_clear()`` calls moved after the ``await`` (the
      pre-PAB-055 ordering): engine new = False, sessionmaker new = False.
    - (b) only the engine cache cleared before the ``await``:
      engine new = True, sessionmaker new = False.
    - (c) only the sessionmaker cache cleared before the ``await``:
      engine new = False, sessionmaker new = True.

    (b) and (c) give *opposite* results on the two identities. A test
    asserting only one of the two identities would pass on whichever
    mutation happens to leave that one identity fresh -- see the ticket
    report for the exact commands and output.

    ``await entered.wait()`` is bounded by ``asyncio.timeout(1)``: a
    mutation that replaces ``await engine.dispose()`` with the synchronous
    ``engine.sync_engine.dispose()`` never goes through the patched
    ``AsyncEngine.dispose`` at all, so ``entered`` is never set and an
    unbounded wait would hang this test (and the whole suite run behind
    it) instead of failing it -- measured on a copy outside the working
    tree (AC14; see the ticket report for the exact command and output).
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
    """AC8: a session obtained before ``dispose_engine()`` keeps its
    original engine binding; that engine is unreachable from the caches
    afterwards. Documented as a *limit* of the guarantee, per the ticket:
    this is the reason PAB-033's shutdown order requires draining
    in-flight session consumers *before* calling ``dispose_engine()``, not
    a claim that in-flight sessions get migrated onto the new engine.
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
    """AC9(b): on an exception raised by the consumer, ``rollback`` is
    called exactly once and the *same* exception object propagates out.

    Mutations demonstrated on copies outside the working tree (AC14):
    dropping ``await session.rollback()`` turns ``rollback_spy.assert_awaited_once()``
    red; dropping the trailing ``raise`` turns the exception into a silent
    ``StopAsyncIteration`` instead of propagating ``RuntimeError``, turning
    ``pytest.raises(RuntimeError)`` red (see the ticket report for the
    exact commands and output).
    """
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
    """AC9(b)/(c): the error path logs one ``ERROR`` record with the exact
    message and ``extra`` fields the ticket names, and the database
    password never reaches that record -- in the message or in ``extra``.

    Positive controls (convention 22), both in this same test: (1) the
    marker is asserted to actually be present in the settings' own
    ``DATABASE_URL`` before the assertion that it is absent from the log
    record, proving this test scenario is genuinely leak-prone and not
    accidentally leak-free; (2) the receiver's own record count is
    asserted to be exactly one before inspecting its fields, proving the
    sink is alive and not structurally incapable of receiving anything
    (the failure mode from PAB-054 review finding 2).

    A custom loguru sink is used instead of ``capsys``/stderr: no
    ``setup_logging()`` runs in this test process, so the stock handler
    would render the extended traceback with ``diagnose=True`` -- not what
    this test is about, and the exact channel Ловушка 2 of PAB-054 warns
    against relying on.

    Mutations demonstrated on copies outside the working tree (AC14):
    changing ``operation="db_session"`` to a different literal turns the
    ``extra["operation"] == "db_session"`` assertion red; replacing
    ``logger.bind(...).exception(...)`` with
    ``logger.bind(...).opt(exception=True).critical(...)`` keeps the
    message and ``extra`` unchanged and would still pass this sink's
    ``level="ERROR"`` filter (which admits ``CRITICAL`` too, since loguru
    filters by minimum severity) -- the explicit level-name assertion
    below is what actually turns that one red (see the ticket report for
    the exact commands and output).
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
