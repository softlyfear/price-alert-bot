"""PAB-099: migration ``a06e7f4948b5`` (``products.price_fetched_at``), live.

The ``db_session`` fixture hands over a schema built by ``alembic upgrade
head``. The revision functions are then run directly on its connection (the
same technique as ``test_schema_live.py``), so every step stays inside the
fixture's rolling-back transaction. DDL is transactional in PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

REVISION_ID = "a06e7f4948b5"
PARENT_REVISION_ID = "a1f3c9d27b64"
REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = ScriptDirectory(str(REPO_ROOT / "alembic"))


def _run_revision(connection: Connection, direction: str) -> None:
    revision = _SCRIPTS.get_revision(REVISION_ID)
    assert revision is not None
    with Operations.context(MigrationContext.configure(connection)):
        getattr(revision.module, direction)()


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection)
    diff: list[Any] = compare_metadata(context, Base.metadata)
    return diff


def _flatten(entries: list[Any]) -> Iterator[Any]:
    for entry in entries:
        if isinstance(entry, list):
            yield from _flatten(entry)
        else:
            yield entry


async def _column_info(session: AsyncSession) -> list[tuple[str, str, str | None]]:
    rows = await session.execute(
        text(
            "SELECT data_type, is_nullable, column_default"
            " FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'products'"
            " AND column_name = 'price_fetched_at'"
        )
    )
    return [(a, b, c) for a, b, c in rows.all()]


async def _insert_product(session: AsyncSession) -> int:
    user_id = (
        await session.execute(
            text("INSERT INTO users (tg_user_id) VALUES (700000099) RETURNING id")
        )
    ).scalar_one()
    product_id = (
        await session.execute(
            text(
                "INSERT INTO products (user_id, marketplace, article, product_name,"
                " current_price) VALUES (:u, 'wb', 99, 'Old row', 100) RETURNING id"
            ),
            {"u": user_id},
        )
    ).scalar_one()
    assert isinstance(product_id, int)
    return product_id


async def _row(session: AsyncSession, product_id: int) -> tuple[str, int]:
    row = (
        await session.execute(
            text("SELECT product_name, current_price FROM products WHERE id = :id"),
            {"id": product_id},
        )
    ).one()
    return row[0], row[1]


def test_revision_follows_the_previous_one() -> None:
    revision = _SCRIPTS.get_revision(REVISION_ID)
    assert revision is not None
    assert revision.down_revision == PARENT_REVISION_ID


@pytest.mark.asyncio
async def test_upgrade_adds_a_nullable_timestamptz_column_without_default_or_backfill(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    # Back to the previous revision, with a row that predates the column.
    await connection.run_sync(_run_revision, "downgrade")
    assert await _column_info(db_session) == []  # positive control: column gone
    product_id = await _insert_product(db_session)

    await connection.run_sync(_run_revision, "upgrade")

    assert await _column_info(db_session) == [("timestamp with time zone", "YES", None)]
    stamp = (
        await db_session.execute(
            text("SELECT price_fetched_at FROM products WHERE id = :id"),
            {"id": product_id},
        )
    ).scalar_one()
    assert stamp is None


@pytest.mark.asyncio
async def test_downgrade_drops_the_column_keeps_rows_and_upgrade_reapplies(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    product_id = await _insert_product(db_session)
    assert len(await _column_info(db_session)) == 1  # head has the column

    await connection.run_sync(_run_revision, "downgrade")

    assert await _column_info(db_session) == []
    assert await _row(db_session, product_id) == ("Old row", 100)

    await connection.run_sync(_run_revision, "upgrade")

    assert await _column_info(db_session) == [("timestamp with time zone", "YES", None)]
    assert await _row(db_session, product_id) == ("Old row", 100)


@pytest.mark.asyncio
async def test_model_and_migrated_schema_agree_on_price_fetched_at(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()

    at_head = list(_flatten(await connection.run_sync(_diff)))

    assert [e for e in at_head if "price_fetched_at" in repr(e)] == []

    # Positive control (convention 22): without the migration the same
    # comparison does report the missing column.
    await connection.run_sync(_run_revision, "downgrade")
    without = list(_flatten(await connection.run_sync(_diff)))
    added = [e for e in without if "price_fetched_at" in repr(e)]
    assert len(added) == 1
    assert added[0][0] == "add_column"
    assert added[0][2] == "products"
    assert added[0][3].name == "price_fetched_at"
