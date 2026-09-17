"""Selection and validation of the PostgreSQL URL for the integration suite.

Two branches, chosen once per test session by ``tests/integration/conftest.py``:

- ``TEST_DATABASE_URL`` is present (non-blank) in the environment -- use it,
  after validating it, and never touch Docker.
- Otherwise -- start a disposable PostgreSQL container via ``testcontainers``
  and derive the connection URL from it.

The branch-selection and validation logic lives in this standalone module,
separated from container startup mechanics and from any actual Docker or
network I/O, specifically so that
``tests/unit/test_integration_database_source.py`` can exercise every
branch -- including the readiness poll below -- with fakes, and never touch
Docker or a real socket.

.. warning::
   ``TEST_DATABASE_URL``, once validated here, is handed straight to
   ``alembic upgrade head`` (see ``tests/integration/conftest.py``). This
   module has no way to tell "a database this suite owns" apart from "a
   database an operator's typo happened to point at" -- a misconfigured
   value runs a real schema migration against whatever it names. This is
   inherent to PAB-011 AC3 (the variable exists specifically so CI/PAB-037
   can point it at a real, already-provisioned database) and is not fixed
   by validation; it is named here so nobody mistakes the checks below for
   a safety net against the wrong database, only against the wrong *shape*
   of URL.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import Protocol

from sqlalchemy.engine import make_url

#: Name of the environment variable carrying an operator-supplied database
#: URL. Fixed here so that PAB-037 (CI) can rely on it without guessing.
TEST_DATABASE_URL_VAR = "TEST_DATABASE_URL"

#: The only driver this project installs a dependency for (see PROJECT.md
#: section 3: "Экстра postgres у testcontainers пуста" -- psycopg2/psycopg
#: are deliberately absent).
REQUIRED_DRIVER = "asyncpg"

_REQUIRED_URL_PREFIX = f"postgresql+{REQUIRED_DRIVER}://"

#: Default PostgreSQL port, used when a URL omits one -- ``asyncpg`` already
#: defaults to it when connecting, so ``build_migration_env`` must too,
#: rather than handing the migration subprocess the literal string
#: ``"None"`` as ``DB__PORT``.
DEFAULT_POSTGRES_PORT = 5432

#: How many *consecutive* successful connections are required before the
#: database is declared ready. More than one is deliberate: PostgreSQL
#: restarts once after running its initdb scripts on first boot, and a
#: single successful connection can land in the window just before that
#: restart, right before the server disappears again. Must stay >= 2 --
#: enforced by test_default_readiness_required_successes_is_at_least_two
#: and test_wait_until_ready_default_requires_more_than_two_successes in
#: tests/unit/test_integration_database_source.py, deliberately *not* by a
#: module-level ``assert`` here: a bare ``assert`` at import time turns a
#: weakened constant into a collection error for the entire test session
#: (``Interrupted: 1 error during collection``) rather than a single
#: failing test, and disappears completely under ``python -O``.
DEFAULT_READINESS_REQUIRED_SUCCESSES = 3
DEFAULT_READINESS_TIMEOUT_SECONDS = 30.0
DEFAULT_READINESS_POLL_INTERVAL_SECONDS = 0.5


def mask_database_url(url: str) -> str:
    """Render ``url`` with any password hidden, safe for error messages.

    Both an operator-supplied ``TEST_DATABASE_URL`` and a freshly started
    container's URL can carry a real password. Falls back to a
    scheme-only rendering if the string does not even parse as a URL,
    rather than echoing anything from the unparseable remainder -- a
    malformed value is exactly the case where blindly printing it back is
    most tempting and most dangerous.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # deliberately catch-all, see docstring
        scheme, separator, _ = url.partition("://")
        return f"{scheme}://<unparseable>" if separator else "<unparseable>"


class DatabaseUrl(str):
    """A database URL string whose ``repr()`` is always password-masked.

    Fixes the actual channel, not one call site: ``pytest`` renders each
    traceback frame's *arguments* -- and, under ``--showlocals``, every
    local variable -- via ``repr()``, not ``str()``. A plain ``str``
    holding a raw URL prints its own unmasked value verbatim in that
    dump regardless of how carefully any exception *message* built from
    it is masked. This was fixed three times, once per function taking a
    URL parameter (``select_database_url``, ``wait_until_ready``,
    ``tests/integration/conftest.py``'s ``_run_migrations``), each fix
    catching only the function already known to raise -- the next
    function added that takes a URL and can fail (and one already did:
    ``_run_migrations``, on a migration failure, the single most likely
    error on a first CI setup) would reopen the same channel. Wrapping
    the value itself at the one place it first exists, instead of
    wrapping every call site that might raise, closes it for any current
    and future function along the way.

    Every other identity is preserved: equality, hashing, string methods,
    use as a ``dict``/``os.environ`` value, and use as a SQLAlchemy or
    ``asyncpg`` connection target all behave exactly like a plain
    ``str``, because none of those go through ``repr()``. Verified
    directly against all three sinks this value actually reaches:
    ``sqlalchemy.ext.asyncio.create_async_engine``, ``asyncpg.connect``,
    and ``subprocess.run(env=...)``.

    Deliberately does *not* suppress the traceback the way
    ``pytest.fail(pytrace=False)`` previously did at each call site: an
    unexpected exception unrelated to the URL (a bug in this module, say)
    still gets its full trace, because nothing here touches how
    tracebacks are rendered -- only what one specific value renders as.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return repr(mask_database_url(self))


class UnsupportedDatabaseDriverError(ValueError):
    """Raised when ``TEST_DATABASE_URL`` does not declare the asyncpg driver.

    Without this check, a URL such as ``postgresql://...`` (SQLAlchemy's
    driverless alias, which resolves to psycopg2) would fail deep inside
    SQLAlchemy with ``ModuleNotFoundError: No module named 'psycopg2'`` --
    a driver this project never installs. Catching the mismatch here
    upfront names the actual requirement instead.
    """

    def __init__(self, url: str) -> None:
        super().__init__(
            f"{TEST_DATABASE_URL_VAR} must use the '{REQUIRED_DRIVER}' driver "
            f"(expected a URL starting with {_REQUIRED_URL_PREFIX!r}, got: "
            f"{mask_database_url(url)!r}). This project only installs "
            "asyncpg -- psycopg2 and psycopg are not dependencies."
        )


class IncompleteDatabaseUrlError(ValueError):
    """Raised when ``TEST_DATABASE_URL`` parses but is unusable downstream.

    ``tests/integration/conftest.py`` decomposes the URL into ``DB__*``
    environment variables, one component at a time, to hand to the
    migration subprocess. Anything not captured by
    username/host/port/database -- most importantly query parameters such
    as ``?ssl=require`` -- silently never reaches ``alembic``, even though
    the readiness poll (which connects with the *whole* URL) sees it just
    fine. Rejecting this upfront turns a confusing downstream ``Settings``
    validation error, or a silently dropped connection option, into one
    diagnosis naming ``TEST_DATABASE_URL`` directly. The message
    deliberately never receives or echoes the raw URL: nothing here can
    leak a password.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"{TEST_DATABASE_URL_VAR} {reason}.")


def validate_asyncpg_url[UrlT: str](url: UrlT) -> UrlT:
    """Return ``url`` unchanged if it is a usable asyncpg URL.

    Checks, in order: the driver is asyncpg; username, host, and database
    name are all present (a missing port is tolerated -- the caller
    defaults it); there are no query parameters, which would be silently
    dropped when building the migration subprocess's environment.

    Generic over ``UrlT`` (bound to ``str``) purely so that passing in a
    ``DatabaseUrl`` gets a ``DatabaseUrl`` back, not a plain ``str`` --
    the type checker, not just the runtime object identity, should know
    the masked wrapper survives validation.
    """
    if not url.startswith(_REQUIRED_URL_PREFIX):
        raise UnsupportedDatabaseDriverError(url)

    parsed = make_url(url)
    missing = [
        name
        for name, value in (
            ("username", parsed.username),
            ("host", parsed.host),
            ("database name", parsed.database),
        )
        if not value
    ]
    if missing:
        raise IncompleteDatabaseUrlError(f"is missing {', '.join(missing)}")
    if parsed.query:
        raise IncompleteDatabaseUrlError(
            f"carries query parameters {sorted(parsed.query)}, which the "
            "migration subprocess silently drops"
        )
    return url


def build_migration_env(url: str) -> dict[str, str]:
    """Build the ``DB__*`` environment variables the migration subprocess needs.

    Only ``DB__USER``, ``DB__PASSWORD``, ``DB__HOST``, ``DB__PORT``, and
    ``DB__NAME`` come from ``url`` -- every other required ``Settings()``
    field (``DB__ECHO``, ``TG__BOT_TOKEN``, and so on) has nothing
    meaningful to derive from a database URL and is supplied separately by
    the caller (``tests/integration/conftest.py``'s ``_FAKE_SETTINGS_ENV``).

    ``DB__PORT`` falls back to ``DEFAULT_POSTGRES_PORT`` when ``url`` omits
    a port -- ``parsed.port`` is ``None`` in that case, and the previous
    form of this code (``str(parsed.port)``) handed the migration
    subprocess the literal string ``"None"``, which ``Settings`` then
    rejected with an ``int_parsing`` error that named nothing about
    ``TEST_DATABASE_URL``. Separated into its own pure function so this can
    be covered by a unit test that never touches a real database (see
    ``tests/unit/test_integration_database_source.py``): the previous
    ``str(parsed.port)`` bug is only observable with a URL that omits a
    port, and every URL used anywhere else in this test suite happens to
    include one.
    """
    parsed = make_url(url)
    return {
        "DB__USER": parsed.username or "",
        "DB__PASSWORD": parsed.password or "",
        "DB__HOST": parsed.host or "",
        "DB__PORT": str(parsed.port or DEFAULT_POSTGRES_PORT),
        "DB__NAME": parsed.database or "",
    }


class StartedContainer(Protocol):
    """The subset of a started ``PostgresContainer`` this module depends on."""

    def get_connection_url(self, *, driver: str) -> str:
        """Return a SQLAlchemy connection URL using the given driver."""
        ...

    def stop(self) -> None:
        """Stop and remove the container."""
        ...


def env_database_url(env: Mapping[str, str]) -> DatabaseUrl | None:
    """Return the operator-supplied URL, or ``None`` if unset or blank.

    Blank -- empty or whitespace-only -- is treated exactly like unset: CI
    matrices commonly spell "not overridden" as ``TEST_DATABASE_URL=""``
    rather than omitting the key entirely. Shared between
    ``select_database_url`` and ``tests/integration/conftest.py``'s
    Docker-availability check so the two never disagree about which
    branch is active.

    Wraps the result as ``DatabaseUrl`` here, at the one place the raw
    value first leaves the environment mapping, rather than leaving that
    to each caller -- this is also why ``select_database_url`` below no
    longer takes the *whole* environment as a parameter: a ``Mapping``
    holding ``os.environ`` would itself print every raw environment
    variable, ``TEST_DATABASE_URL`` included, under ``pytest --showlocals``
    on any exception raised while that mapping is a live parameter,
    regardless of what ``DatabaseUrl`` does for the extracted value alone.
    """
    raw = (env.get(TEST_DATABASE_URL_VAR) or "").strip()
    return DatabaseUrl(raw) if raw else None


def select_database_url(
    raw_url: str | None,
    start_container: Callable[[], StartedContainer],
) -> tuple[DatabaseUrl, StartedContainer | None]:
    """Resolve the database URL for this test session.

    ``raw_url`` is the already-extracted value of ``TEST_DATABASE_URL``
    (see ``env_database_url``), not the environment mapping itself --
    deliberately, so that this function's own frame never holds a
    parameter carrying the whole environment (see ``env_database_url``'s
    docstring for why that matters under ``--showlocals``).

    Returns ``(url, container)``. ``container`` is ``None`` when
    ``raw_url`` was given -- there is nothing for the caller to stop, and
    ``start_container`` is never invoked. Otherwise ``container`` is
    whatever ``start_container()`` returned (expected to already be
    started), and the caller owns stopping it once the session is done.

    The parameter is re-wrapped as ``DatabaseUrl`` on the very first line,
    even though ``env_database_url`` already returns one: the local name
    ``raw_url`` is itself a parameter of this frame, bound to whatever the
    caller passed in *before* this line runs, so skipping the rewrap would
    leave that earlier binding as a plain, unmasked ``str`` for the
    lifetime of this call if the caller passed one directly (as some unit
    tests do, for convenience).

    ``start_container`` is a plain callable rather than a direct reference
    to ``testcontainers.community.postgres.PostgresContainer`` so that unit
    tests can substitute a fake that never touches Docker.
    """
    if raw_url is not None:
        raw_url = DatabaseUrl(raw_url)
        return validate_asyncpg_url(raw_url), None
    container = start_container()
    url = container.get_connection_url(driver=REQUIRED_DRIVER)
    return DatabaseUrl(url), container


async def wait_until_ready(
    url: str,
    connect: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_READINESS_POLL_INTERVAL_SECONDS,
    required_consecutive_successes: int = DEFAULT_READINESS_REQUIRED_SUCCESSES,
) -> None:
    """Call ``connect()`` until it succeeds several times in a row, or time out.

    ``connect`` is injected -- rather than this function hardcoding
    ``asyncpg.connect`` -- so it can be unit-tested with a fake that never
    opens a socket (see
    ``tests/unit/test_integration_database_source.py``); the caller in
    ``tests/integration/conftest.py`` binds the real database URL into it
    via a closure and is responsible for closing whatever ``connect()``
    returns.

    A single success is not enough: see ``DEFAULT_READINESS_REQUIRED_SUCCESSES``.
    A failure resets the streak, so the requirement is genuinely
    *consecutive*, not merely "N successes somewhere in the budget".

    ``url`` is used only to render a masked identifier in the eventual
    ``TimeoutError`` -- never anything unmasked, and never anything from
    ``connect`` itself, which may carry credentials of its own.
    """
    deadline = time.monotonic() + timeout_seconds
    consecutive_successes = 0
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            connection = await connect()
        except Exception as exc:  # any failure here just means "not ready yet"
            last_error = exc
            consecutive_successes = 0
        else:
            close = getattr(connection, "close", None)
            if close is not None:
                await close()
            consecutive_successes += 1
            if consecutive_successes >= required_consecutive_successes:
                return
        await asyncio.sleep(poll_interval_seconds)
    last_error_type = type(last_error).__name__ if last_error is not None else None
    raise TimeoutError(
        f"PostgreSQL at {mask_database_url(url)!r} did not accept "
        f"{required_consecutive_successes} consecutive connections within "
        f"{timeout_seconds}s (last error type: {last_error_type})"
    )
