"""PAB-103: migration ``eafcd2321542`` (``products.initial_price``), live.

Same technique as ``test_price_fetched_at_migration_live.py``: the
``db_session`` fixture hands over a schema built by ``alembic upgrade head``
and the revision functions run directly on its connection, so every step stays
inside the fixture's rolling-back transaction (DDL is transactional in
PostgreSQL).
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

REVISION_ID = "eafcd2321542"
PARENT_REVISION_ID = "a06e7f4948b5"
CONSTRAINT = "ck_products_initial_price_non_negative"
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
            " WHERE table_schema = 'public' AND table_name = 'products'"
            " AND column_name = 'initial_price'"
        )
    )
    return [(a, b, c) for a, b, c in rows.all()]


async def _constraints(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        text(
            "SELECT conname FROM pg_constraint"
            " WHERE conrelid = 'products'::regclass AND conname = :n"
        ),
        {"n": CONSTRAINT},
    )
    return [r[0] for r in rows.all()]


async def _insert_product(session: AsyncSession, *, with_initial: int | None) -> int:
    user_id = (
        await session.execute(
            text("INSERT INTO users (tg_user_id) VALUES (700000103) RETURNING id")
        )
    ).scalar_one()
    columns = "user_id, marketplace, article, product_name, current_price"
    values = ":u, 'wb', 103, 'Old row', 100"
    if with_initial is not None:
        columns += ", initial_price"
        values += f", {with_initial}"
    product_id = (
        await session.execute(
            text(f"INSERT INTO products ({columns}) VALUES ({values}) RETURNING id"),
            {"u": user_id},
        )
    ).scalar_one()
    assert isinstance(product_id, int)
    return product_id


def test_revision_follows_the_previous_one() -> None:
    revision = _SCRIPTS.get_revision(REVISION_ID)
    assert revision is not None
    assert revision.down_revision == PARENT_REVISION_ID


def test_revision_is_on_the_chain_of_the_single_head() -> None:
    heads = _SCRIPTS.get_heads()
    assert len(heads) == 1
    chain = [r.revision for r in _SCRIPTS.walk_revisions(base="base", head=heads[0])]
    assert REVISION_ID in chain


@pytest.mark.asyncio
async def test_upgrade_adds_a_nullable_integer_column_and_leaves_old_rows_null(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    await connection.run_sync(_run_revision, "downgrade")
    assert await _column_info(db_session) == []  # positive control
    assert await _constraints(db_session) == []
    product_id = await _insert_product(db_session, with_initial=None)

    await connection.run_sync(_run_revision, "upgrade")

    assert await _column_info(db_session) == [("integer", "YES", None)]
    assert await _constraints(db_session) == [CONSTRAINT]
    stored = (
        await db_session.execute(
            text("SELECT initial_price FROM products WHERE id = :id"),
            {"id": product_id},
        )
    ).scalar_one()
    assert stored is None


@pytest.mark.asyncio
async def test_upgrade_downgrade_upgrade_cycle_keeps_rows(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    product_id = await _insert_product(db_session, with_initial=None)
    assert len(await _column_info(db_session)) == 1  # head has the column

    await connection.run_sync(_run_revision, "downgrade")

    assert await _column_info(db_session) == []
    assert await _constraints(db_session) == []
    name = (
        await db_session.execute(
            text("SELECT product_name FROM products WHERE id = :id"),
            {"id": product_id},
        )
    ).scalar_one()
    assert name == "Old row"

    await connection.run_sync(_run_revision, "upgrade")

    assert await _column_info(db_session) == [("integer", "YES", None)]
    assert await _constraints(db_session) == [CONSTRAINT]


@pytest.mark.asyncio
async def test_negative_initial_price_is_rejected_by_the_named_constraint(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await _insert_product(db_session, with_initial=-1)

    assert CONSTRAINT in str(excinfo.value)


@pytest.mark.asyncio
async def test_zero_initial_price_is_accepted(db_session: AsyncSession) -> None:
    product_id = await _insert_product(db_session, with_initial=0)

    stored = (
        await db_session.execute(
            text("SELECT initial_price FROM products WHERE id = :id"),
            {"id": product_id},
        )
    ).scalar_one()
    assert stored == 0
