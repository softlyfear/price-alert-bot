"""PAB-084: ``alembic upgrade head`` with special characters in role and password.

A role name and password holding a space, ``%``, ``@``, ``/``, ``<`` (and ``:``
in the password) are percent-encoded in the database URL. Before PAB-084 the
``%`` of that encoding crashed ``configparser`` interpolation inside
``alembic/env.py`` with the whole DSN, password included, in the exception text.

Every case runs ``alembic`` in a fresh subprocess with the ``DB__*``
environment, like ``tests/integration/conftest.py::_run_migrations``, against
a throwaway role and database created here and dropped in ``finally``. The admin
credentials of ``TEST_DATABASE_URL`` are held only as ``DatabaseUrl``. DDL goes
through server-side ``format()`` with ``%I`` / ``%L``: bind parameters do not
work in ``CREATE ROLE`` / ``CREATE DATABASE``.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote
from urllib.parse import quote_plus

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from tests.integration.conftest import _FAKE_SETTINGS_ENV
from tests.integration.conftest import MIGRATION_TIMEOUT_SECONDS
from tests.integration.conftest import REPO_ROOT
from tests.integration.conftest import _connect_asyncpg
from tests.integration.database_source import DatabaseUrl


@dataclass(frozen=True)
class _Target:
    """Throwaway role and database on the live server.

    ``password`` is a fictitious value generated here, not a secret: the
    admin credentials of ``TEST_DATABASE_URL`` never live in this class.
    """

    host: str
    port: int
    role: str
    password: str
    database: str
    password_token: str


async def _connect_admin(admin_url: DatabaseUrl) -> asyncpg.Connection:
    """Connect as the ``TEST_DATABASE_URL`` role; the URL stays masked.

    A failed connect is re-raised without the original traceback:
    ``asyncpg`` frames hold the password as a plain ``str`` local, and
    ``--showlocals`` would print it.
    """
    try:
        return cast("asyncpg.Connection", await _connect_asyncpg(admin_url))
    except Exception as exc:
        failure = type(exc).__name__
    raise RuntimeError(f"admin connection failed: {failure}") from None


async def _connect_target(target: _Target) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=target.host,
        port=target.port,
        user=target.role,
        password=target.password,
        database=target.database,
    )


async def _can_create(admin_url: DatabaseUrl) -> bool:
    """Whether the admin role may create a role and a database."""
    conn = await _connect_admin(admin_url)
    try:
        return bool(
            await conn.fetchval(
                "SELECT rolsuper OR (rolcreaterole AND rolcreatedb)"
                " FROM pg_roles WHERE rolname = current_user"
            )
        )
    finally:
        await conn.close()


async def _create(admin_url: DatabaseUrl, target: _Target) -> None:
    """Create role and database as the admin; DDL via server-side ``format()``."""
    conn = await _connect_admin(admin_url)
    try:
        role_ddl = await conn.fetchval(
            "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', $1::text, $2::text)",
            target.role,
            target.password,
        )
        db_ddl = await conn.fetchval(
            "SELECT format('CREATE DATABASE %I OWNER %I', $1::text, $2::text)",
            target.database,
            target.role,
        )
        await conn.execute(role_ddl)
        await conn.execute(db_ddl)
    finally:
        await conn.close()


async def _drop(admin_url: DatabaseUrl, target: _Target) -> None:
    """Remove whatever of the database and the role exists; safe on a partial setup."""
    conn = await _connect_admin(admin_url)
    try:
        drop_db = await conn.fetchval(
            "SELECT format('DROP DATABASE IF EXISTS %I WITH (FORCE)', $1::text)",
            target.database,
        )
        drop_role = await conn.fetchval(
            "SELECT format('DROP ROLE IF EXISTS %I', $1::text)", target.role
        )
        await conn.execute(drop_db)
        await conn.execute(drop_role)
    finally:
        await conn.close()


async def _stored_revisions(target: _Target) -> list[str]:
    conn = await _connect_target(target)
    try:
        rows = await conn.fetch("SELECT version_num FROM alembic_version")
        return [str(row["version_num"]) for row in rows]
    finally:
        await conn.close()


def _require_create(admin_url: DatabaseUrl) -> None:
    """Skip the test unless the admin role may create roles and databases.

    Called before any DDL as the admin, and outside any ``try``/``finally``
    cleanup: ``pytest.skip`` is an exception, and a failing cleanup would
    replace it, turning a skip into an ``ERROR``.
    """
    if not asyncio.run(_can_create(admin_url)):
        pytest.skip(
            "database role of TEST_DATABASE_URL lacks CREATEROLE/CREATEDB;"
            " cannot create the throwaway role and database"
        )


@contextmanager
def _throwaway(admin_url: DatabaseUrl) -> Iterator[_Target]:
    """Create a throwaway role and database, drop them on exit.

    Privileges are checked before anything is created and outside the
    ``try``: ``pytest.skip`` is an exception, and a cleanup in ``finally``
    that fails (``DROP ROLE`` without ``CREATEROLE`` is ``permission
    denied``) would replace it, turning a skip into an ``ERROR``.
    """
    _require_create(admin_url)
    parsed = make_url(admin_url)
    suffix = uuid.uuid4().hex[:12]
    token = uuid.uuid4().hex
    created = _Target(
        host=parsed.host or "",
        port=parsed.port or 5432,
        role=f"role %@/< {suffix}",
        password=f"pw @%/<: {token}",
        database=f"pab084_{suffix}",
        password_token=token,
    )
    try:
        asyncio.run(_create(admin_url, created))
        yield created
    finally:
        asyncio.run(_drop(admin_url, created))


@pytest.fixture
def target(database_url: str) -> Iterator[_Target]:
    with _throwaway(DatabaseUrl(database_url)) as created:
        yield created


def _env(
    target: _Target, *, port: int | None = None, password: str | None = None
) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "DB__USER": target.role,
        "DB__PASSWORD": target.password if password is None else password,
        "DB__HOST": target.host,
        "DB__PORT": str(target.port if port is None else port),
        "DB__NAME": target.database,
        **_FAKE_SETTINGS_ENV,
    }


def _alembic(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=MIGRATION_TIMEOUT_SECONDS,
        check=False,
    )


def _head() -> str:
    head = ScriptDirectory.from_config(
        Config(str(REPO_ROOT / "alembic.ini"))
    ).get_current_head()
    assert head is not None
    return head


def _sqlalchemy_quote(text: str) -> str:
    return re.sub(r"[:@/]", lambda m: f"%{ord(m.group(0)):X}", text)


def _password_forms(password: str) -> list[str]:
    """Raw, percent-encoded (several encoders), and each with ``%`` doubled."""
    base = [
        password,
        quote(password, safe=""),
        quote_plus(password),
        _sqlalchemy_quote(password),
    ]
    return base + [form.replace("%", "%%") for form in base]


def _assert_no_password(output: str, secret: str, token: str) -> None:
    for form in _password_forms(secret):
        assert form not in output
    assert token not in output


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_upgrade_head_succeeds_with_special_characters_in_role_and_password(
    target: _Target,
) -> None:
    result = _alembic(_env(target))

    assert result.returncode == 0, result.stderr.replace(target.password_token, "***")
    assert asyncio.run(_stored_revisions(target)) == [_head()]


def test_wrong_password_fails_with_authentication_error_and_without_password(
    target: _Target,
) -> None:
    wrong_token = uuid.uuid4().hex
    wrong = f"wrong @%/<: {wrong_token}"

    result = _alembic(_env(target, password=wrong))

    output = result.stdout + result.stderr
    assert result.returncode != 0
    # Positive control: the refusal is the authentication one, so the
    # absence checks below are not run on an unrelated crash.
    assert "password authentication failed" in output
    _assert_no_password(output, wrong, wrong_token)
    _assert_no_password(output, target.password, target.password_token)


def test_closed_port_fails_with_connection_error_and_without_password(
    target: _Target,
) -> None:
    result = _alembic(_env(target, port=_free_port()))

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert re.search(r"Connect call failed|Connection refused", output)
    _assert_no_password(output, target.password, target.password_token)


def test_offline_sql_mode_succeeds_with_special_characters_and_prints_revision_sql(
    target: _Target,
) -> None:
    result = _alembic(_env(target), "--sql")

    assert result.returncode == 0, result.stderr.replace(target.password_token, "***")
    assert "CREATE TABLE" in result.stdout
    assert f"'{_head()}'" in result.stdout
    _assert_no_password(
        result.stdout + result.stderr, target.password, target.password_token
    )


# --- PAB-084 attempt 1: skip branch and admin-password masking -----------------


async def _names(admin_url: DatabaseUrl) -> tuple[set[str], set[str]]:
    conn = await _connect_admin(admin_url)
    try:
        roles = {str(r[0]) for r in await conn.fetch("SELECT rolname FROM pg_roles")}
        dbs = {str(r[0]) for r in await conn.fetch("SELECT datname FROM pg_database")}
        return roles, dbs
    finally:
        await conn.close()


async def _exec_ident(admin_url: DatabaseUrl, template: str, *args: str) -> None:
    conn = await _connect_admin(admin_url)
    try:
        await conn.execute(await conn.fetchval(template, *args))
    finally:
        await conn.close()


_NO_PRIVILEGES = "NOCREATEROLE NOCREATEDB"

#: Role attributes for ``unprivileged_url``: lacking both rights, and holding
#: exactly one of them. ``_can_create`` needs *both*; each single-right role
#: must still be refused. Closed set of literals, not input.
_ROLE_ATTRIBUTES = [
    pytest.param(_NO_PRIVILEGES, id="neither"),
    pytest.param("CREATEROLE NOCREATEDB", id="createrole_only"),
    pytest.param("NOCREATEROLE CREATEDB", id="createdb_only"),
]


@pytest.fixture
def unprivileged_url(
    database_url: str, request: pytest.FixtureRequest
) -> Iterator[DatabaseUrl]:
    """URL of a ``LOGIN NOSUPERUSER`` role owning its own database.

    Role attributes come from ``request.param`` (indirect parametrization)
    and default to ``NOCREATEROLE NOCREATEDB``. Owning the database lets a
    whole nested pytest session (with its own migrations) run against it,
    like a managed-database role in CI.
    """
    attributes = cast("str", getattr(request, "param", _NO_PRIVILEGES))
    admin_url = DatabaseUrl(database_url)
    _require_create(admin_url)
    suffix = uuid.uuid4().hex[:12]
    role = f"pab084_np_{suffix}"
    database = f"pab084_npdb_{suffix}"
    password = uuid.uuid4().hex
    try:
        asyncio.run(
            _exec_ident(
                admin_url,
                "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER %s PASSWORD %L',"
                " $1::text, $3::text, $2::text)",
                role,
                password,
                attributes,
            )
        )
        asyncio.run(
            _exec_ident(
                admin_url,
                "SELECT format('CREATE DATABASE %I OWNER %I', $1::text, $2::text)",
                database,
                role,
            )
        )
        yield DatabaseUrl(
            make_url(admin_url)
            .set(username=role, password=password, database=database)
            .render_as_string(hide_password=False)
        )
    finally:
        asyncio.run(
            _exec_ident(
                admin_url,
                "SELECT format('DROP DATABASE IF EXISTS %I WITH (FORCE)', $1::text)",
                database,
            )
        )
        asyncio.run(
            _exec_ident(
                admin_url, "SELECT format('DROP ROLE IF EXISTS %I', $1::text)", role
            )
        )


@pytest.mark.parametrize("unprivileged_url", _ROLE_ATTRIBUTES, indirect=True)
def test_role_without_create_privileges_gives_skip_and_leaves_no_roles_or_databases(
    unprivileged_url: DatabaseUrl,
) -> None:
    before = asyncio.run(_names(unprivileged_url))

    with (
        pytest.raises(pytest.skip.Exception, match="lacks CREATEROLE/CREATEDB"),
        _throwaway(unprivileged_url),
    ):
        pytest.fail("the body must not run without privileges")

    assert asyncio.run(_names(unprivileged_url)) == before


def test_drop_removes_role_left_without_database(database_url: str) -> None:
    admin_url = DatabaseUrl(database_url)
    _require_create(admin_url)
    suffix = uuid.uuid4().hex[:12]
    lone = _Target(
        host="",
        port=0,
        role=f"pab084_lone_{suffix}",
        password="x",
        database=f"pab084_nodb_{suffix}",
        password_token="x",
    )
    asyncio.run(
        _exec_ident(
            admin_url,
            "SELECT format('CREATE ROLE %I', $1::text)",
            lone.role,
        )
    )
    try:
        asyncio.run(_drop(admin_url, lone))
        roles, dbs = asyncio.run(_names(admin_url))
    finally:
        asyncio.run(
            _exec_ident(
                admin_url,
                "SELECT format('DROP ROLE IF EXISTS %I', $1::text)",
                lone.role,
            )
        )

    assert lone.role not in roles
    assert lone.database not in dbs


_PROBE = """
import tests.integration.test_migrations_live as live
from tests.integration.database_source import DatabaseUrl
from tests.integration.test_migrations_live import target  # noqa: F401
import pytest


@pytest.fixture
def database_url():
    return DatabaseUrl("postgresql+asyncpg://adm:{marker}@127.0.0.1:1/db")


{patches}

def test_probe(target):
    pass
"""


_FORCED_FAILURE_PATCHES = """
async def _always_allowed(admin_url):
    return True


async def _boom(admin_url, created):
    raise RuntimeError("forced failure inside the fixture")


async def _noop(admin_url, created):
    return None


live._can_create = _always_allowed
live._create = _boom
live._drop = _noop
"""

#: No patches: the admin connect to a closed port fails inside the fixture.
_UNREACHABLE_PATCHES = ""


@pytest.mark.parametrize(
    "flags",
    [["-q"], ["--showlocals", "--tb=long"]],
    ids=["quiet", "showlocals_long"],
)
@pytest.mark.parametrize(
    ("patches", "positive_control"),
    [
        pytest.param(
            _FORCED_FAILURE_PATCHES, "forced failure inside the fixture", id="forced"
        ),
        pytest.param(
            _UNREACHABLE_PATCHES, "admin connection failed", id="admin_unreachable"
        ),
    ],
)
def test_admin_password_absent_from_output_when_fixture_fails(
    tmp_path: Path, flags: list[str], patches: str, positive_control: str
) -> None:
    marker = f"adm-pw-{uuid.uuid4().hex}"
    probe = tmp_path / "test_probe_admin_password.py"
    probe.write_text(_PROBE.format(marker=marker, patches=patches))

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-p", "no:cacheprovider", *flags],
        cwd=str(REPO_ROOT),
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=MIGRATION_TIMEOUT_SECONDS,
        check=False,
    )

    output = result.stdout + result.stderr
    # Positive control: the run failed inside the fixture, as forced.
    assert result.returncode != 0
    assert positive_control in output
    if "--showlocals" in flags:
        assert "admin_url" in output
    assert marker not in output


_DDL_TESTS = [
    "test_upgrade_head_succeeds_with_special_characters_in_role_and_password",
    "test_wrong_password_fails_with_authentication_error_and_without_password",
    "test_closed_port_fails_with_connection_error_and_without_password",
    "test_offline_sql_mode_succeeds_with_special_characters_and_prints_revision_sql",
    "test_role_without_create_privileges_gives_skip_and_leaves_no_roles_or_databases",
    "test_drop_removes_role_left_without_database",
    "test_whole_file_skips_without_errors_on_role_lacking_create_privileges",
]

_SUMMARY_LINE = re.compile(r"^=+ .* in [\d.]+s.*=+$")
_BAD_COUNT = re.compile(r"\b(\d+) (failed|errors?)\b")


def _bad_outcome_counts(output: str) -> dict[str, int]:
    """Counts of ``failed`` / ``error`` outcomes from the final summary line.

    Read by the outcome word, not by cutting substrings out of the output:
    removing ``"0 error"`` would also hide the tail of ``"10 errors"``.
    """
    summaries = [ln for ln in output.splitlines() if _SUMMARY_LINE.match(ln)]
    assert summaries, f"no pytest summary line in: {output[-500:]}"
    counts: dict[str, int] = {}
    for number, word in _BAD_COUNT.findall(summaries[-1]):
        counts[word.rstrip("s")] = int(number)
    return counts


def _result_lines(output: str, name: str) -> list[str]:
    """Every ``-v`` result line of test ``name``, parametrized ids included."""
    pattern = re.compile(rf"::{re.escape(name)}(\[[^\]]*\])? ")
    return [ln for ln in output.splitlines() if pattern.search(ln)]


def test_bad_outcome_counts_reads_the_count_by_outcome_word() -> None:
    assert _bad_outcome_counts("=== 3 passed, 10 errors in 1.50s ===") == {"error": 10}
    assert _bad_outcome_counts("=== 1 failed, 2 skipped in 0.10s ===") == {"failed": 1}
    assert _bad_outcome_counts("=== 9 skipped in 0.10s ===") == {}


def test_bad_outcome_counts_refuses_output_without_summary() -> None:
    with pytest.raises(AssertionError, match="no pytest summary line"):
        _bad_outcome_counts("collected 0 items")


def test_result_lines_match_plain_and_parametrized_names_only() -> None:
    output = (
        "f.py::test_a SKIPPED [ 10%]\n"
        "f.py::test_a[x] SKIPPED [ 20%]\n"
        "f.py::test_ab SKIPPED [ 30%]\n"
    )

    assert _result_lines(output, "test_a") == [
        "f.py::test_a SKIPPED [ 10%]",
        "f.py::test_a[x] SKIPPED [ 20%]",
    ]
    assert _result_lines(output, "test_missing") == []


def test_whole_file_skips_without_errors_on_role_lacking_create_privileges(
    unprivileged_url: DatabaseUrl, tmp_path: Path
) -> None:
    """Every test issuing DDL as the admin is skipped, none fails or errors.

    The file is run in a subprocess with ``TEST_DATABASE_URL`` pointing at a
    ``LOGIN NOCREATEROLE NOCREATEDB`` role. Inside that run this very test
    skips at its own fixture, so the recursion ends there. A new DDL step
    without a privilege check turns one of the named tests into a failure.
    The environment copy and the password are never bound to locals: a
    failure of this test under ``--showlocals`` would print them.
    """
    assert make_url(unprivileged_url).password

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__).resolve()),
            "-v",
            "-rs",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "inner"),
        ],
        cwd=str(REPO_ROOT),
        env={**os.environ, "TEST_DATABASE_URL": str(unprivileged_url)},
        capture_output=True,
        text=True,
        timeout=MIGRATION_TIMEOUT_SECONDS,
        check=False,
    )

    output = result.stdout + result.stderr
    assert str(make_url(unprivileged_url).password) not in output
    assert result.returncode == 0, output[-2000:]
    assert _bad_outcome_counts(output) == {}
    for name in _DDL_TESTS:
        lines = _result_lines(output, name)
        assert lines, f"no result line for {name}"
        for line in lines:
            assert "SKIPPED" in line, line
    assert "lacks CREATEROLE/CREATEDB" in output
