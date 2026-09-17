"""Fixtures for integration tests against a live PostgreSQL instance.

**Database source.** ``TEST_DATABASE_URL``, if set to a non-blank value, is
used directly (after validation -- see
``tests/integration/database_source.py``); otherwise a disposable
PostgreSQL container is started via ``testcontainers``. If the env var is
absent/blank *and* the Docker daemon is unreachable, the session is
skipped with a message naming both the cause and ``TEST_DATABASE_URL`` as
the way out (PAB-011 AC8). If the env var *is* set, no such skip path
exists: an unreachable database is a hard failure, surfaced through the
readiness poll below.

.. warning::
   A misconfigured ``TEST_DATABASE_URL`` runs a real ``alembic upgrade
   head`` against whatever it names -- there is no way for this fixture to
   distinguish a database the suite owns from one an operator's typo
   happened to point at. See the warning in
   ``tests/integration/database_source.py`` for why this is inherent to
   AC3 and not something validation can close.

**Readiness.** Whichever URL is resolved, this module polls it with
``asyncpg`` -- the same driver the application uses -- before doing
anything else with it, requiring several *consecutive* successful
connections (``database_source.DEFAULT_READINESS_REQUIRED_SUCCESSES``).
A single successful ``docker exec``-based check (as
``PostgresContainer._connect`` already performs for the container branch)
does not prove the *asyncpg* path is usable, and a lone successful
connection can land in the window before PostgreSQL's post-initdb
restart. An operator-supplied ``TEST_DATABASE_URL`` has no ``docker
exec``-based check at all. The poll has a finite timeout; exhausting it
raises ``TimeoutError`` instead of hanging the run.

**Schema.** Built by running ``alembic upgrade head`` against the resolved
URL -- never SQLAlchemy's bulk schema-creation helper on ``Base.metadata``.
This is the one point where the distinction is load-bearing for this
ticket: PAB-011 condition (e) is a claim about what the migration in
``alembic/versions/c55fadc558a3_*.py`` actually creates in PostgreSQL, and
building tables straight from current model metadata would silently turn
the check into a tautology.

Migrations run in a **subprocess**, exactly once per test session. The
reason is ``alembic/env.py`` itself: it calls ``get_settings()`` (an
``lru_cache``d, argument-less function) at import time and overwrites
``sqlalchemy.url`` from whatever it returns, so
``Config.set_main_option("sqlalchemy.url", ...)`` is always clobbered --
the only lever left is the process environment ``get_settings()`` reads.
Setting that environment on *this* process would work for the migration
call, but would also permanently poison ``get_settings()``'s cache and
``app/core/database.py``'s engine/sessionmaker caches for the rest of the
test session (PAB-011 AC4) -- there is no supported way to evict a
specific ``Settings()`` build from ``lru_cache`` short of calling
``get_settings.cache_clear()``, and doing that reliably around every
fixture that might race with it is exactly the kind of fragile plumbing a
subprocess avoids by construction. The price, paid once per session, is
one extra interpreter start; see the report for the measured cost.

**Isolation.** Each test gets its own connection with a real transaction
opened before the test and rolled back after it, per SQLAlchemy's
documented "Joining a Session into an External Transaction" pattern. The
session is built with ``join_transaction_mode="create_savepoint"``, so
code under test remains free to call ``session.commit()`` -- it only
releases a SAVEPOINT and immediately opens a new one -- without its
changes ever escaping the outer, connection-level transaction that gets
rolled back at teardown. This also names the limit of the strategy: if a
test drives a ``flush()`` into a failure on purpose (PAB-058 does this),
the *session's* internal transactional state can become unusable for
further ORM calls on that same session, but isolation itself does not
depend on that state -- cleanup discards everything through the
connection-level transaction regardless of what shape the session was
left in.

**Event loop.** No *asynchronous* fixture here raises its scope above
``function`` -- ``db_session`` is function-scoped, matching the default
(unset) ``loop_scope`` on ``@pytest.mark.asyncio`` in this project's
tests. ``database_url`` is session-scoped but entirely synchronous (see
its docstring), so it is not bound to any event loop at all and this
alignment question does not apply to it. An ``asyncpg`` connection
created on one event loop and used from another raises at the driver
level, so getting this right for ``db_session`` is load-bearing, not
incidental -- see the report for the explicit statement required by
PAB-011 AC11.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.docker_client import DockerClient

from tests.integration.database_source import DatabaseUrl
from tests.integration.database_source import build_migration_env
from tests.integration.database_source import env_database_url
from tests.integration.database_source import select_database_url
from tests.integration.database_source import wait_until_ready

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Pinned explicitly -- "latest" would make the schema-under-test depend on
#: whatever image happens to be cached (PAB-011 AC10).
POSTGRES_IMAGE = "postgres:17.6-alpine"

MIGRATION_TIMEOUT_SECONDS = 60.0

#: Fictitious values for every ``DB__*``/``TG__*``/``REDIS__*`` variable that
#: ``Settings()`` requires and that has no default (PAB-011 AC3). None of
#: these resemble a real credential.
_FAKE_SETTINGS_ENV: dict[str, str] = {
    "DB__ECHO": "false",
    "DB__POOL_SIZE": "5",
    "DB__MAX_OVERFLOW": "0",
    "DB__POOL_PRE_PING": "false",
    "DB__POOL_RECYCLE": "3600",
    "DB__AUTOFLUSH": "false",
    "DB__EXPIRE_ON_COMMIT": "false",
    "TG__BOT_TOKEN": "test-suite-placeholder-token",
    "REDIS__HOST": "localhost",
}


def _docker_daemon_available() -> bool:
    """Return whether the Docker daemon answers, without raising.

    Goes through ``testcontainers``'s own client wrapper rather than
    importing the ``docker`` package directly: ``docker`` is not declared
    in ``pyproject.toml`` (dependency management is ``devops-agent``'s
    zone) and arrives here only transitively through ``testcontainers``.
    A direct ``import docker`` at module level would make the whole
    ``tests/integration`` package fail to even collect the moment
    ``testcontainers`` stops depending on it or changes its exception
    hierarchy. Catching bare ``Exception`` around client construction and
    the ping call keeps this check from depending on any specific
    exception class at all -- any failure here degrades to "treat Docker
    as unavailable", never an import-time crash of the whole package. The
    client is explicitly closed either way: it opens a requests session
    that otherwise outlives this function.
    """
    try:
        client = DockerClient()
    except Exception:
        return False
    try:
        with contextlib.closing(client.client):
            client.client.ping()
    except Exception:
        return False
    return True


def _start_postgres_container() -> PostgresContainer:
    """Start a disposable PostgreSQL container.

    ``driver=None`` disarms ``PostgresContainer``'s default
    ``get_connection_url()`` driver (``psycopg2``, not installed here); the
    caller always passes ``driver="asyncpg"`` explicitly at the call site
    instead (PAB-011 AC9). Readiness is *not* this function's job -- the
    class's own ``_connect()`` already blocks ``.start()`` on a successful
    ``psql`` probe run inside the container via ``docker exec``, which
    survives the well-known postgres-restarts-after-initdb hazard because
    it retries the whole probe, not a single connection attempt.
    """
    container = PostgresContainer(POSTGRES_IMAGE, driver=None)
    container.start()
    return container


def _run_migrations(url: str) -> None:
    """Apply ``alembic upgrade head`` in a fresh interpreter against ``url``.

    See the module docstring for why this is a subprocess rather than an
    in-process ``alembic.command.upgrade`` call. ``url`` has already been
    validated by ``database_source.validate_asyncpg_url`` by the time it
    reaches here for the ``TEST_DATABASE_URL`` branch (username, host, and
    database name are all present, and there are no query parameters that
    this decomposition would otherwise silently drop); the container
    branch always supplies all of these by construction.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        **build_migration_env(url),
        **_FAKE_SETTINGS_ENV,
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=MIGRATION_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head failed with rc="
            f"{result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


async def _connect_asyncpg(url: str) -> asyncpg.connection.Connection:
    """Open one asyncpg connection to ``url``, for use by ``wait_until_ready``.

    ``dsn`` is re-wrapped as ``DatabaseUrl`` rather than left as the plain
    ``str`` that ``str.replace()`` returns (subclassing ``str`` does not
    make its own methods return the subclass) -- this is itself a local
    holding the full URL, in a function that can raise, and would leak
    under ``--showlocals`` exactly like the ``url`` parameters this class
    was introduced to protect (see ``DatabaseUrl``'s docstring).
    """
    dsn = DatabaseUrl(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    return await asyncpg.connect(dsn)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Resolve, migrate, and tear down (if applicable) the test database.

    Runs once per session. Synchronous on purpose: container start/stop and
    the migration subprocess are both blocking calls with no need for an
    event loop, so tying this fixture's scope to a specific loop (a
    session-scoped async fixture would have to) is avoided entirely -- see
    the module docstring, "Event loop". The readiness poll is itself
    async, so it is driven with a throwaway ``asyncio.run()`` that starts
    and closes its own loop entirely within this function -- it never
    shares a loop with any test.

    No failure path here is wrapped in ``try/except`` for the sake of
    masking a password: ``select_database_url`` returns a
    ``database_source.DatabaseUrl`` (a ``str`` subclass whose ``repr()``
    is always masked, see its docstring), so ``url`` stays masked in
    every subsequent frame's argument dump -- including
    ``_run_migrations``'s, on a migration failure, and including under
    ``--showlocals`` -- without this fixture needing to know in advance
    which of the functions it calls might raise. An exception unrelated
    to the URL keeps its full, unsuppressed traceback, unlike an earlier
    version of this fixture that caught ``ValueError``/``TimeoutError``
    and re-raised through ``pytest.fail(pytrace=False)`` at each call
    site -- that approach worked, but only for the two failure points
    known at the time; measured by
    ``test_database_url_fixture_output_never_contains_the_raw_password``
    in ``test_live_database.py``, parametrized over all three current
    failure points, which asserts against the actual ``pytest`` output
    text, not ``str(exc)``.
    """
    # `os.environ` is read through `env_database_url` twice, immediately,
    # rather than bound to a local once -- a local holding `os.environ`
    # itself (not just the one value extracted from it) would print every
    # raw environment variable, `TEST_DATABASE_URL` included, under
    # `pytest --showlocals` on any exception raised anywhere in this
    # function. See `env_database_url`'s docstring.
    if env_database_url(os.environ) is None and not _docker_daemon_available():
        pytest.skip(
            "Docker daemon is unreachable and TEST_DATABASE_URL is not "
            "set (or is blank); set "
            "TEST_DATABASE_URL=postgresql+asyncpg://... to run these "
            "tests without Docker."
        )

    url, container = select_database_url(
        env_database_url(os.environ), _start_postgres_container
    )
    try:
        asyncio.run(wait_until_ready(url, lambda: _connect_asyncpg(url)))
        _run_migrations(url)
        yield url
    finally:
        if container is not None:
            container.stop()


@pytest_asyncio.fixture
async def db_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Provide a session isolated by an outer, per-test transaction rollback.

    Function-scoped, matching the default loop scope of
    ``@pytest.mark.asyncio`` test functions in this project (see the module
    docstring, "Event loop"). A fresh engine per test is deliberate: it
    keeps this fixture from ever outliving the event loop of the test it
    serves, at the cost of one extra connection setup per test -- cheap
    against a local/CI PostgreSQL instance.
    """
    engine = create_async_engine(database_url)
    try:
        connection = await engine.connect()
        transaction = await connection.begin()
        try:
            session = AsyncSession(
                bind=connection,
                autoflush=False,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                await session.close()
        finally:
            await transaction.rollback()
            await connection.close()
    finally:
        await engine.dispose()
