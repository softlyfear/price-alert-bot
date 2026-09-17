"""Smoke tests against a real PostgreSQL instance, plus fixture self-checks.

PAB-011 condition (e): PROJECT.md section 6 proves, at the level of
SQLAlchemy metadata and the migration file's own text, that the PostgreSQL
enum type ``marketplace`` is declared with ``('wb', 'ozon')`` -- but names
the explicit limit that this does *not* establish what a live database
actually contains after running the migration.
``test_ozon_value_round_trips_and_pg_enum_contains_exactly_wb_and_ozon``
closes that gap: it runs against a database built by
``alembic upgrade head`` (never a bulk schema build from current model
metadata, see ``tests/integration/conftest.py``) and reads the enum's
member set back
from PostgreSQL's own system catalog, not from ``Base.metadata``.

The remaining tests in this file are not about condition (e) -- they prove
the fixture itself behaves as PAB-011's acceptance criteria require
(production-matching session flags, rollback isolation across tests), so
that PAB-058 can build repository tests on top of it without re-deriving
those guarantees.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Marketplace
from app.models import Product
from app.models import User

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Query PostgreSQL's own system catalog for the members of a native enum
#: type -- deliberately not ``Base.metadata``, which would make this a
#: tautology instead of a check on what the migration actually built.
_ENUM_LABELS_SQL = text(
    """
    SELECT e.enumlabel
    FROM pg_type t
    JOIN pg_enum e ON t.oid = e.enumtypid
    WHERE t.typname = :type_name
    ORDER BY e.enumsortorder
    """
)


@pytest.mark.asyncio
async def test_ozon_value_round_trips_and_pg_enum_contains_exactly_wb_and_ozon(
    db_session: AsyncSession,
) -> None:
    """Condition (e): the live ``marketplace`` PG enum is exactly {wb, ozon}."""
    user = User(tg_user_id=900_100_200)
    db_session.add(user)
    await db_session.flush()

    product = Product(
        user_id=user.id,
        marketplace=Marketplace.ozon,
        article=1_234_567,
        product_name="PAB-011 smoke product",
        current_price=1_000,
    )
    db_session.add(product)
    await db_session.flush()

    # (a) Read back through a plain SQL SELECT, bypassing the ORM identity
    # map, so this genuinely observes what PostgreSQL stored rather than
    # the Python object still held in memory.
    result_a = await db_session.execute(
        text("SELECT marketplace FROM products WHERE id = :id"),
        {"id": product.id},
    )
    assert result_a.scalar_one() == "ozon"

    # (b) The PG type itself, read from the system catalog -- not from
    # Base.metadata, which would prove nothing about the migration.
    result_b = await db_session.execute(_ENUM_LABELS_SQL, {"type_name": "marketplace"})
    assert set(result_b.scalars()) == {"wb", "ozon"}

    # Positive control for (b): the type must actually *restrict* values,
    # not merely happen to contain the two we expect -- otherwise a plain
    # ``varchar`` column would pass the assertion above just as well. A
    # value outside {wb, ozon} must be rejected by PostgreSQL itself.
    with pytest.raises(DBAPIError):
        await db_session.execute(text("SELECT 'not_a_real_marketplace'::marketplace"))


@pytest.mark.asyncio
async def test_db_session_matches_production_session_flags(
    db_session: AsyncSession,
) -> None:
    """AC5: the fixture's session carries the same flags as `.env.example` / prod.

    Read from the live session object, not asserted by comment -- PAB-058's
    tests for `flush()`-related repository behaviour (PROJECT.md section
    8.3) are only meaningful if ``autoflush`` here actually matches
    production.
    """
    assert db_session.autoflush is False
    assert db_session.sync_session.autoflush is False
    assert db_session.sync_session.expire_on_commit is False


@pytest.mark.asyncio
async def test_transaction_rollback_isolation_witness_a(
    db_session: AsyncSession,
) -> None:
    """AC6, half 1 of 2: this test's row must not survive into another test.

    Both halves insert a ``User`` with the *same* unique ``tg_user_id``. If
    the previous test's row leaked past its own rollback, this insert would
    raise a unique-constraint violation regardless of which half runs
    first -- that is the actual failure mode being guarded against, not
    just the row count below.

    This half calls ``session.commit()`` deliberately, simulating a
    production caller (e.g. ``DbSessionMiddleware``) driving the
    transaction boundary. With ``join_transaction_mode="create_savepoint"``
    this only releases a SAVEPOINT -- isolation between tests then depends
    entirely on the fixture's *outer*, connection-level rollback, not on
    ``AsyncSession.close()``'s implicit rollback of an uncommitted session.
    Measured on a copy outside the repository (see the report): with the
    outer rollback replaced by a commit, this test still passes on its own,
    but `..._witness_b` then fails on the leaked row -- proving the outer
    rollback, not the session-level one, is what this half actually
    exercises.
    """
    tg_user_id = 777_000_111

    before = await db_session.execute(
        select(User.id).where(User.tg_user_id == tg_user_id)
    )
    assert before.scalars().all() == []  # absence check

    db_session.add(User(tg_user_id=tg_user_id))
    await db_session.commit()

    after = await db_session.execute(
        select(User.id).where(User.tg_user_id == tg_user_id)
    )
    assert len(after.scalars().all()) == 1  # positive control: query does find rows


@pytest.mark.asyncio
async def test_transaction_rollback_isolation_witness_b(
    db_session: AsyncSession,
) -> None:
    """AC6, half 2 of 2 -- see `..._witness_a` docstring."""
    tg_user_id = 777_000_111

    before = await db_session.execute(
        select(User.id).where(User.tg_user_id == tg_user_id)
    )
    assert before.scalars().all() == []

    db_session.add(User(tg_user_id=tg_user_id))
    await db_session.flush()

    after = await db_session.execute(
        select(User.id).where(User.tg_user_id == tg_user_id)
    )
    assert len(after.scalars().all()) == 1


@pytest.mark.asyncio
async def test_isolation_survives_a_failed_flush_left_unrolled_back(
    db_session: AsyncSession,
) -> None:
    """AC6 limit, half 1 of 2: a failed ``flush()`` need not be followed by
    ``session.rollback()`` for the fixture to clean up correctly.

    Deliberately violates the unique constraint on ``User.tg_user_id`` and
    lets the resulting ``IntegrityError`` propagate out of the ``with``
    block without ever calling ``db_session.rollback()`` -- this is
    exactly the shape of test PAB-058 will write on purpose against
    repository methods that surface constraint violations. PostgreSQL now
    considers this session's transaction unusable for further statements;
    the point of this pair is that the *fixture's* teardown (the
    connection-level rollback in ``tests/integration/conftest.py``) does
    not depend on this session ever being repaired, and does not itself
    raise while cleaning up a session left in this state.

    Ordering dependency, named explicitly: this test must run before
    ``..._sees_a_clean_database_after_the_previous_test``, or the second
    half is not exercising anything. Both are collected from the same
    module in declaration order, and no test-order-randomizing plugin is
    installed in this project. There is no rollback call anywhere in this
    test.
    """
    tg_user_id = 424_242_424
    db_session.add(User(tg_user_id=tg_user_id))
    db_session.add(User(tg_user_id=tg_user_id))  # duplicate -> unique violation

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_sees_a_clean_database_after_the_previous_test(
    db_session: AsyncSession,
) -> None:
    """AC6 limit, half 2 of 2 -- see the previous test's docstring.

    A fresh ``db_session`` here proves the previous test's unrolled-back,
    broken session did not survive into this one: if the fixture's
    connection-level rollback had somehow been skipped, or had itself
    raised and aborted teardown, either this session would fail to set up
    at all, or the duplicate row above would still be visible.
    """
    tg_user_id = 424_242_424

    result = await db_session.execute(
        select(User.id).where(User.tg_user_id == tg_user_id)
    )
    assert result.scalars().all() == []


#: Scenario 1: ``select_database_url`` rejects the driver before anything
#: touches a socket -- no monkeypatching needed, and fast regardless of
#: Docker availability.
_BAD_DRIVER_URL = "postgresql://probe:{marker}@127.0.0.1:5432/probe"
_BAD_DRIVER_PATCH = ""

#: Scenario 2: ``wait_until_ready`` times out. ``_connect_asyncpg`` is
#: replaced with one that always fails instantly (no real socket, no real
#: delay per attempt), and ``wait_until_ready``'s own keyword-only defaults
#: are shrunk in place via ``__kwdefaults__`` -- the ``database_url``
#: fixture calls it without overriding them, so this is the only way to
#: keep the *default* readiness budget from costing this test 30 real
#: seconds while still exercising the actual call the fixture makes.
_READINESS_TIMEOUT_URL = "postgresql+asyncpg://probe:{marker}@127.0.0.1:5432/probe"
_READINESS_TIMEOUT_PATCH = """
from tests.integration import conftest as integration_conftest


async def _always_fails(url):
    raise ConnectionRefusedError("simulated: never ready (regression test)")


integration_conftest._connect_asyncpg = _always_fails
integration_conftest.wait_until_ready.__kwdefaults__ = {
    "timeout_seconds": 0.05,
    "poll_interval_seconds": 0.01,
    "required_consecutive_successes": 3,
}
"""

#: Scenario 3: ``_run_migrations`` raises. This is the path Major 2's
#: report missed: a migration failure (wrong privileges, drifted revision,
#: wrong schema -- the single most likely failure on a first CI setup) is
#: reached only *after* readiness succeeds, so readiness is faked as an
#: instant success here rather than skipped, keeping the call shape the
#: fixture actually uses.
_MIGRATION_FAILURE_URL = "postgresql+asyncpg://probe:{marker}@127.0.0.1:5432/probe"
_MIGRATION_FAILURE_PATCH = """
from tests.integration import conftest as integration_conftest


async def _instant_ready(url, connect, **kwargs):
    return None


def _failing_migrations(url):
    raise RuntimeError("simulated: alembic upgrade head failed (regression test)")


integration_conftest.wait_until_ready = _instant_ready
integration_conftest._run_migrations = _failing_migrations
"""


@pytest.mark.parametrize(
    ("url_template", "conftest_patch", "positive_control"),
    [
        pytest.param(
            _BAD_DRIVER_URL, _BAD_DRIVER_PATCH, "TEST_DATABASE_URL", id="bad_driver"
        ),
        pytest.param(
            _READINESS_TIMEOUT_URL,
            _READINESS_TIMEOUT_PATCH,
            "did not accept",
            id="readiness_timeout",
        ),
        pytest.param(
            _MIGRATION_FAILURE_URL,
            _MIGRATION_FAILURE_PATCH,
            "alembic upgrade head failed",
            id="migration_failure",
        ),
    ],
)
def test_database_url_fixture_output_never_contains_the_raw_password(
    tmp_path: Path,
    url_template: str,
    conftest_patch: str,
    positive_control: str,
) -> None:
    """Regression test: measures ``pytest``'s actual output, not ``str(exc)``.

    Asserting against ``str(exc_info.value)`` (as this project's other
    password-leak guards do) misses a real channel: an *uncaught*
    exception's traceback is rendered by ``pytest`` frame by frame, and,
    under ``--showlocals``, so is every local variable -- both via
    ``repr()``, regardless of how carefully any exception *message* is
    masked. This was fixed, and this test broken, three times over: once
    per failure path inside ``database_url`` (``select_database_url``,
    ``wait_until_ready``, ``_run_migrations``). Parametrized over all
    three so that a fourth failure path introduced later has to be added
    here explicitly to pass, rather than silently reopening the same
    channel the way ``_run_migrations`` once did.

    Each parametrization runs a genuinely separate ``pytest`` process
    against a throwaway test module in ``tmp_path`` (never inside
    ``tests/``, so nothing lingers in the suite -- see the project's rule
    against probes committed under ``tests/``) that requests the
    ``database_url`` fixture and does nothing else. A local ``conftest.py``
    is written alongside it for the scenarios that need to monkeypatch a
    fast, socket-free failure into ``tests.integration.conftest`` (whose
    module object it imports and mutates directly -- picked up because
    the real ``database_url`` fixture body looks up ``_connect_asyncpg``,
    ``wait_until_ready``, and ``_run_migrations`` as plain module globals
    at call time, not at import time). ``tests/integration/conftest.py``
    itself is loaded explicitly as a plugin by dotted path, since the
    temporary module does not live under a directory that would pick it
    up by pytest's normal conftest discovery; pytest loads that explicit
    plugin before collecting the local ``conftest.py``, so the patch
    always lands after the real definitions exist.
    """
    marker = "s3cr3t-regression-marker-pass"
    bad_url = url_template.format(marker=marker)
    probe_module = tmp_path / "test_probe_database_url_fixture.py"
    probe_module.write_text("def test_placeholder(database_url):\n    pass\n")
    if conftest_patch:
        (tmp_path / "conftest.py").write_text(conftest_patch)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe_module),
            "-p",
            "tests.integration.conftest",
            "-q",
        ],
        cwd=_REPO_ROOT,
        env={**os.environ, "TEST_DATABASE_URL": bad_url},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert marker not in result.stdout
    assert marker not in result.stderr
    assert positive_control in result.stdout  # positive control
    assert result.returncode != 0  # a hard failure, not a silent skip
