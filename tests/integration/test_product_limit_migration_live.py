"""PAB-105: migration ``b7d4e91c5a30`` (``users.product_limit``), live.

Same technique as ``test_initial_price_migration_live.py``: the revision
functions run on the ``db_session`` connection, inside the rolling-back
transaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

REVISION_ID = "b7d4e91c5a30"
PARENT_REVISION_ID = "eafcd2321542"
CONSTRAINT = "ck_users_product_limit_positive"
REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = ScriptDirectory(str(REPO_ROOT / "alembic"))


def _run_revision(connection: Connection, direction: str) -> None:
    revision = _SCRIPTS.get_revision(REVISION_ID)
    assert revision is not None
    with Operations.context(MigrationContext.configure(connection)):
        getattr(revision.module, direction)()


async def _column_info(session: AsyncSession) -> list[tuple[str, str, str | None]]:
    rows = await session.execute(
        text(
            "SELECT data_type, is_nullable, column_default"
            " FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'users'"
            " AND column_name = 'product_limit'"
        )
    )
    return [(a, b, c) for a, b, c in rows.all()]


async def _constraints(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT conname FROM pg_constraint"
            " WHERE conrelid = 'users'::regclass AND conname = :n"
        ),
        {"n": CONSTRAINT},
    )
    return [r[0] for r in rows.all()]


async def _insert_user(session: AsyncSession, tg: int, limit: int | None) -> int:
    if limit is None:
        sql = "INSERT INTO users (tg_user_id) VALUES (:tg) RETURNING id"
    else:
        sql = (
            "INSERT INTO users (tg_user_id, product_limit)"
            f" VALUES (:tg, {limit}) RETURNING id"
        )
    user_id = (await session.execute(text(sql), {"tg": tg})).scalar_one()
    assert isinstance(user_id, int)
    return user_id


def test_revision_follows_the_initial_price_one() -> None:
    revision = _SCRIPTS.get_revision(REVISION_ID)
    assert revision is not None
    assert revision.down_revision == PARENT_REVISION_ID


def test_revision_is_the_single_head() -> None:
    assert _SCRIPTS.get_heads() == [REVISION_ID]


@pytest.mark.asyncio
async def test_upgrade_adds_a_nullable_integer_column_and_existing_user_is_null(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    await connection.run_sync(_run_revision, "downgrade")
    assert await _column_info(db_session) == []  # positive control
    assert await _constraints(db_session) == []
    user_id = await _insert_user(db_session, 700_000_105, None)

    await connection.run_sync(_run_revision, "upgrade")

    assert await _column_info(db_session) == [("integer", "YES", None)]
    assert await _constraints(db_session) == [CONSTRAINT]
    stored = (
        await db_session.execute(
            text("SELECT product_limit FROM users WHERE id = :id"), {"id": user_id}
        )
    ).scalar_one()
    assert stored is None


@pytest.mark.asyncio
async def test_upgrade_downgrade_upgrade_cycle_keeps_users(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    user_id = await _insert_user(db_session, 700_000_106, 5)

    await connection.run_sync(_run_revision, "downgrade")

    assert await _column_info(db_session) == []
    assert await _constraints(db_session) == []
    tg = (
        await db_session.execute(
            text("SELECT tg_user_id FROM users WHERE id = :id"), {"id": user_id}
        )
    ).scalar_one()
    assert tg == 700_000_106

    await connection.run_sync(_run_revision, "upgrade")

    assert await _column_info(db_session) == [("integer", "YES", None)]
    assert await _constraints(db_session) == [CONSTRAINT]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1])
async def test_non_positive_limit_is_rejected_by_the_named_constraint(
    db_session: AsyncSession, bad: int
) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert_user(db_session, 700_000_107, bad)

    assert CONSTRAINT in str(excinfo.value)


@pytest.mark.asyncio
async def test_limit_of_one_is_accepted(db_session: AsyncSession) -> None:
    user_id = await _insert_user(db_session, 700_000_108, 1)

    stored = (
        await db_session.execute(
            text("SELECT product_limit FROM users WHERE id = :id"), {"id": user_id}
        )
    ).scalar_one()
    assert stored == 1
