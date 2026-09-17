"""Unit tests for ``tests.integration.database_source``.

These tests must never touch Docker or the network: the container branch is
exercised with a fake stand-in for a started ``PostgresContainer``, and the
env-var branch is exercised through ``env_database_url`` on plain dicts.
This is the paired unit test required by PAB-011 so that both branches of
the database-source selector are proven without depending on the fixtures
in ``tests/integration/``.
"""

from __future__ import annotations

import pytest

from tests.integration.database_source import DEFAULT_POSTGRES_PORT
from tests.integration.database_source import DEFAULT_READINESS_REQUIRED_SUCCESSES
from tests.integration.database_source import TEST_DATABASE_URL_VAR
from tests.integration.database_source import DatabaseUrl
from tests.integration.database_source import IncompleteDatabaseUrlError
from tests.integration.database_source import UnsupportedDatabaseDriverError
from tests.integration.database_source import build_migration_env
from tests.integration.database_source import env_database_url
from tests.integration.database_source import mask_database_url
from tests.integration.database_source import select_database_url
from tests.integration.database_source import wait_until_ready

#: A marker chosen to look nothing like real test data, so any appearance
#: of it in an error message unambiguously means "the password leaked".
_SECRET_MARKER = "s3cr3t-marker-pass"


class _FakeContainer:
    """Stand-in for a started ``PostgresContainer``. Never touches Docker."""

    def __init__(self, url: str) -> None:
        self._url = url
        self.requested_drivers: list[str] = []

    def get_connection_url(self, *, driver: str) -> str:
        self.requested_drivers.append(driver)
        return self._url

    def stop(self) -> None:
        """Never called in these tests -- present only to satisfy the protocol."""


def _forbidden_start_container() -> _FakeContainer:
    raise AssertionError(
        "start_container must not be called when TEST_DATABASE_URL is set"
    )


def test_select_database_url_uses_env_value_without_starting_container() -> None:
    """The env-var branch must win outright and never touch the factory."""
    url = "postgresql+asyncpg://user:pass@example.invalid:5432/db"

    resolved_url, container = select_database_url(
        env_database_url({TEST_DATABASE_URL_VAR: url}), _forbidden_start_container
    )

    assert resolved_url == url
    assert container is None


def test_select_database_url_rejects_env_value_with_wrong_driver() -> None:
    """A driverless/psycopg2-style URL is rejected before it reaches SQLAlchemy."""
    bad_url = "postgresql://user:pass@example.invalid:5432/db"

    with pytest.raises(UnsupportedDatabaseDriverError, match="asyncpg"):
        select_database_url(
            env_database_url({TEST_DATABASE_URL_VAR: bad_url}),
            _forbidden_start_container,
        )


def test_select_database_url_starts_container_when_raw_url_is_none() -> None:
    """With no resolved URL, exactly one container is started and its URL is used."""
    fake = _FakeContainer("postgresql+asyncpg://test:test@127.0.0.1:55000/test")
    calls = 0

    def start_container() -> _FakeContainer:
        nonlocal calls
        calls += 1
        return fake

    resolved_url, container = select_database_url(None, start_container)

    assert resolved_url == fake._url
    assert container is fake
    assert calls == 1
    assert fake.requested_drivers == ["asyncpg"]


def test_select_database_url_masks_repr_even_when_caller_passes_a_plain_str() -> None:
    """Defence in depth: ``raw_url`` is re-wrapped even if not already masked.

    ``env_database_url`` (the only caller in ``tests/integration/conftest.py``)
    already returns a ``DatabaseUrl``, so this matters only for a caller
    that bypasses it and passes a plain ``str`` directly -- verified here
    because nothing else in this suite exercises that path, and
    ``select_database_url``'s own docstring claims the parameter is
    re-wrapped regardless.
    """
    plain_str_url = f"postgresql+asyncpg://user:{_SECRET_MARKER}@host:5432/db"
    assert not isinstance(plain_str_url, DatabaseUrl)

    resolved_url, _container = select_database_url(
        plain_str_url, _forbidden_start_container
    )

    assert isinstance(resolved_url, DatabaseUrl)
    assert _SECRET_MARKER not in repr(resolved_url)


@pytest.mark.parametrize("blank_value", ["", "   ", "\t\n"])
def test_env_database_url_treats_blank_value_as_absent(blank_value: str) -> None:
    """An empty or whitespace-only TEST_DATABASE_URL resolves to ``None``.

    CI matrices commonly spell "not overridden" as ``VAR=""`` rather than
    omitting the key -- treating that the same as a missing key is the
    fix for Minor 5. Checked here at the source (``env_database_url``)
    rather than through ``select_database_url``, since blank-handling
    lives entirely in the former now.
    """
    assert env_database_url({TEST_DATABASE_URL_VAR: blank_value}) is None


def test_env_database_url_returns_a_database_url_instance() -> None:
    """The extracted value is masked at the source, not left as a plain ``str``.

    ``select_database_url`` relies on this: it only re-wraps its
    ``raw_url`` parameter defensively, it does not perform the initial
    extraction from the environment itself.
    """
    resolved = env_database_url(
        {TEST_DATABASE_URL_VAR: "postgresql+asyncpg://user:pass@host:5432/db"}
    )

    assert isinstance(resolved, DatabaseUrl)


def test_unsupported_driver_error_does_not_echo_the_password() -> None:
    """The driver-mismatch diagnosis must not leak a password from the URL."""
    bad_url = f"postgresql://probe:{_SECRET_MARKER}@example.invalid:5432/db"

    with pytest.raises(UnsupportedDatabaseDriverError) as exc_info:
        select_database_url(
            env_database_url({TEST_DATABASE_URL_VAR: bad_url}),
            _forbidden_start_container,
        )

    assert _SECRET_MARKER not in str(exc_info.value)
    assert "TEST_DATABASE_URL" in str(exc_info.value)  # positive control


_NO_USERNAME_URL = f"postgresql+asyncpg://:{_SECRET_MARKER}@example.invalid:5432/db"
_NO_DATABASE_URL = f"postgresql+asyncpg://probe:{_SECRET_MARKER}@example.invalid:5432/"
_QUERY_PARAMS_URL = (
    f"postgresql+asyncpg://probe:{_SECRET_MARKER}@example.invalid:5432/db?ssl=require"
)


@pytest.mark.parametrize("url", [_NO_USERNAME_URL, _NO_DATABASE_URL, _QUERY_PARAMS_URL])
def test_incomplete_url_is_rejected_without_echoing_the_password(url: str) -> None:
    """A URL missing a required part, or carrying dropped query params, is rejected.

    None of these messages ever receive the raw URL at all (see
    ``IncompleteDatabaseUrlError``), so this also serves as a regression
    guard against that changing.
    """
    with pytest.raises(IncompleteDatabaseUrlError) as exc_info:
        select_database_url(
            env_database_url({TEST_DATABASE_URL_VAR: url}), _forbidden_start_container
        )

    assert _SECRET_MARKER not in str(exc_info.value)
    assert "TEST_DATABASE_URL" in str(exc_info.value)  # positive control


def test_mask_database_url_hides_the_password_but_keeps_the_rest() -> None:
    """Masking must hide only the password -- host/db stay visible for diagnosis."""
    masked = mask_database_url(
        f"postgresql+asyncpg://probe:{_SECRET_MARKER}@example.invalid:5432/db"
    )

    assert _SECRET_MARKER not in masked
    assert "example.invalid" in masked
    assert "probe" in masked
    assert "db" in masked


def test_mask_database_url_falls_back_to_scheme_for_unparseable_input() -> None:
    """An unparseable string is never echoed -- not even partially."""
    masked = mask_database_url(f"not a url at all {_SECRET_MARKER}")

    assert _SECRET_MARKER not in masked
    assert masked == "<unparseable>"


def test_database_url_masks_repr_but_preserves_every_other_identity() -> None:
    """``repr()`` is masked; everything that is not ``repr()`` is unaffected.

    ``str()``, equality, and the raw value must all survive unchanged --
    real connections (``asyncpg.connect``, ``create_async_engine``,
    ``subprocess.run(env=...)``) depend on getting the actual password,
    not the masked one.
    """
    raw = f"postgresql+asyncpg://probe:{_SECRET_MARKER}@host:5432/db"
    url = DatabaseUrl(raw)

    assert _SECRET_MARKER not in repr(url)
    assert _SECRET_MARKER in str(url)
    assert url == raw


class _FakeConnection:
    """Stand-in for whatever ``asyncpg.connect`` returns. Never touches a socket."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_wait_until_ready_requires_consecutive_successes() -> None:
    """A single success is not enough; a failure resets the streak.

    Sequence: fail, succeed, succeed, fail, succeed, succeed, succeed.
    With ``required_consecutive_successes=3``, the function must not
    return before the 7th call -- if it returned after the first three
    calls (fail, succeed, succeed) or simply counted *any* three
    successes rather than three in a row, this would under-count calls.
    """
    outcomes = [False, True, True, False, True, True, True]
    calls = 0

    async def fake_connect() -> _FakeConnection:
        nonlocal calls
        succeeded = outcomes[calls]
        calls += 1
        if not succeeded:
            raise ConnectionRefusedError("simulated: not ready yet")
        return _FakeConnection()

    await wait_until_ready(
        "postgresql+asyncpg://test:test@127.0.0.1:5432/test",
        fake_connect,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
        required_consecutive_successes=3,
    )

    assert calls == len(outcomes)


@pytest.mark.asyncio
async def test_wait_until_ready_times_out_without_echoing_the_password() -> None:
    """Exhausting the budget raises TimeoutError with the password masked."""

    async def always_fails() -> _FakeConnection:
        raise ConnectionRefusedError("simulated: never ready")

    url = f"postgresql+asyncpg://probe:{_SECRET_MARKER}@127.0.0.1:5432/probe"

    with pytest.raises(TimeoutError) as exc_info:
        await wait_until_ready(
            url,
            always_fails,
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            required_consecutive_successes=3,
        )

    assert _SECRET_MARKER not in str(exc_info.value)
    assert "did not accept" in str(exc_info.value)  # positive control


@pytest.mark.asyncio
async def test_wait_until_ready_default_requires_more_than_two_successes() -> None:
    """Locks the shipped default itself, not just the counting algorithm.

    ``required_consecutive_successes`` is intentionally *not* passed here:
    ``test_wait_until_ready_requires_consecutive_successes`` above already
    proves the algorithm counts consecutively for whatever value is
    passed in, but the live fixture in ``tests/integration/conftest.py``
    calls ``wait_until_ready`` without overriding it at all -- only
    ``DEFAULT_READINESS_REQUIRED_SUCCESSES`` protects it. This test feeds
    exactly two successes and then failures forever: with the shipped
    default of 3, that must time out. If the default were ever weakened
    to 2 (or 1), this would return successfully instead, and
    ``pytest.raises`` below would fail with "DID NOT RAISE".
    """
    calls = 0

    async def two_successes_then_failures() -> _FakeConnection:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return _FakeConnection()
        raise ConnectionRefusedError("simulated: no more successes available")

    with pytest.raises(TimeoutError):
        await wait_until_ready(
            "postgresql+asyncpg://test:test@127.0.0.1:5432/test",
            two_successes_then_failures,
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )

    assert calls >= 3  # positive control: the two successes did happen


def test_default_readiness_required_successes_is_at_least_two() -> None:
    """A single success can land in the window before postgres's post-initdb
    restart -- see the constant's own comment. This is the *only* place
    that invariant is checked: a module-level ``assert`` was considered
    and rejected, because it turns a weakened constant into
    ``Interrupted: 1 error during collection`` for the entire test
    session (measured on a copy) rather than one failing test here, and
    disappears entirely under ``python -O``.
    """
    assert DEFAULT_READINESS_REQUIRED_SUCCESSES >= 2


def test_build_migration_env_uses_url_port_when_present() -> None:
    """DB__PORT, DB__USER, DB__PASSWORD, DB__HOST, DB__NAME all come from the URL."""
    env = build_migration_env("postgresql+asyncpg://user:pass@myhost:6543/mydb")

    assert env == {
        "DB__USER": "user",
        "DB__PASSWORD": "pass",
        "DB__HOST": "myhost",
        "DB__PORT": "6543",
        "DB__NAME": "mydb",
    }


def test_build_migration_env_defaults_port_when_url_has_none() -> None:
    """Regression guard for Major 2: a portless URL must not become DB__PORT="None".

    Before the fix, this built ``str(parsed.port)`` directly, and
    ``parsed.port`` is ``None`` for a URL that omits one -- ``Settings``
    then rejected the literal string ``"None"`` with an ``int_parsing``
    error naming nothing about ``TEST_DATABASE_URL``.
    """
    env = build_migration_env("postgresql+asyncpg://user:pass@myhost/mydb")

    assert env["DB__PORT"] == str(DEFAULT_POSTGRES_PORT)
    assert env["DB__PORT"] != "None"  # positive control against the original bug
