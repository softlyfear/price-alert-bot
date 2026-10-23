"""Migration ``d3a8c61f0b27`` (``products.card_price``), live.

Same technique as ``test_product_limit_migration_live.py``: the revision
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

REVISION_ID = "d3a8c61f0b27"
PARENT_REVISION_ID = "b7d4e91c5a30"
CONSTRAINT = "ck_products_card_price_non_negative"
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
            " AND column_name = 'card_price'"
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


async def _insert_product(session: AsyncSession, tg: int) -> int:
    user_id = (
        await session.execute(
            text("INSERT INTO users (tg_user_id) VALUES (:tg) RETURNING id"),
            {"tg": tg},
        )
    ).scalar_one()
    product_id = (
        await session.execute(
            text(
                "INSERT INTO products (user_id, marketplace, article, product_name,"
                " current_price) VALUES (:u, 'ozon', 1, 'p', 49500) RETURNING id"
            ),
            {"u": user_id},
        )
    ).scalar_one()
    assert isinstance(product_id, int)
    return product_id


def test_revision_follows_the_product_limit_one_and_is_the_single_head() -> None:
    revision = _SCRIPTS.get_revision(REVISION_ID)
    assert revision is not None
    assert revision.down_revision == PARENT_REVISION_ID
    assert _SCRIPTS.get_heads() == [REVISION_ID]


@pytest.mark.asyncio
async def test_downgrade_then_upgrade_keeps_products_and_the_column_is_null(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    await connection.run_sync(_run_revision, "downgrade")
    assert await _column_info(db_session) == []  # positive control
    assert await _constraints(db_session) == []
    product_id = await _insert_product(db_session, 700_000_201)

    await connection.run_sync(_run_revision, "upgrade")

    assert await _column_info(db_session) == [("integer", "YES", None)]
    assert await _constraints(db_session) == [CONSTRAINT]
    stored = (
        await db_session.execute(
            text("SELECT card_price FROM products WHERE id = :id"), {"id": product_id}
        )
    ).scalar_one()
    assert stored is None


@pytest.mark.asyncio
async def test_negative_card_price_is_rejected_by_the_named_constraint(
    db_session: AsyncSession,
) -> None:
    product_id = await _insert_product(db_session, 700_000_202)

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE products SET card_price = -1 WHERE id = :id"),
                {"id": product_id},
            )

    assert CONSTRAINT in str(excinfo.value)
