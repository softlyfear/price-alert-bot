"""PAB-066: server-side UTC timestamps and price ``CHECK`` constraints, live.

Every test runs on the schema built by ``alembic upgrade head`` (the
``database_url`` / ``db_session`` fixtures), never on ``create_all``.

``now()`` is the start of the current transaction and is constant inside
it, so the timestamp tests can demand exact equality with ``now()`` read in
the same transaction. The session ``TimeZone`` is switched with ``SET
LOCAL``, which the fixture's rollback discards together with everything else.

The catalog is compared with the model metadata, and both sides with a
literal: an empty-to-empty comparison would pass for a schema that has
neither.
"""

from __future__ import annotations

import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint
from sqlalchemy import Connection
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import DefaultClause
from sqlalchemy.sql.elements import TextClause

from app.models.base import Base
from app.models.user import User

REVISION_ID = "a1f3c9d27b64"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: A fixed non-zero UTC offset (+10:00) without daylight saving time.
SESSION_ZONE = "Asia/Vladivostok"

TABLES = ("users", "products", "alerts")
TIMESTAMP_COLUMNS = tuple((t, c) for t in TABLES for c in ("created_at", "updated_at"))

EXPECTED_CHECKS = {
    ("products", "ck_products_current_price_non_negative"),
    ("products", "ck_products_previous_price_non_negative"),
    ("products", "ck_products_initial_price_non_negative"),
    ("products", "ck_products_card_price_non_negative"),
    ("alerts", "ck_alerts_target_price_non_negative"),
    ("users", "ck_users_product_limit_positive"),
}

_INSERT_USER = "INSERT INTO users (tg_user_id) VALUES (:tg) RETURNING id"
_INSERT_PRODUCT = (
    "INSERT INTO products (user_id, marketplace, article, product_name,"
    " current_price, previous_price)"
    " VALUES (:user_id, 'wb', :article, 'p', :current, :previous) RETURNING id"
)
_INSERT_ALERT = (
    "INSERT INTO alerts (user_id, product_id, target_price, direction)"
    " VALUES (:user_id, :product_id, :target, 'below') RETURNING id"
)


async def _scalar_int(session: AsyncSession, sql: str, **params: int) -> int:
    value = (await session.execute(text(sql), params)).scalar_one()
    assert isinstance(value, int)
    return value


async def _make_user(session: AsyncSession) -> int:
    return await _scalar_int(session, _INSERT_USER, tg=700_000_001)


async def _make_product(session: AsyncSession, user_id: int) -> int:
    return await _scalar_int(
        session,
        "INSERT INTO products (user_id, marketplace, article, product_name,"
        " current_price) VALUES (:user_id, 'wb', 1, 'p', 100) RETURNING id",
        user_id=user_id,
    )


async def _catalog_defaults(session: AsyncSession) -> dict[tuple[str, str], str]:
    rows = await session.execute(
        text(
            "SELECT c.relname, a.attname, pg_get_expr(d.adbin, d.adrelid)"
            " FROM pg_attrdef d"
            " JOIN pg_class c ON c.oid = d.adrelid"
            " JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum"
            " WHERE c.relnamespace = 'public'::regnamespace"
            " AND c.relname IN ('users', 'products', 'alerts')"
            " AND a.attname IN ('created_at', 'updated_at')"
        )
    )
    return {(table, column): expr for table, column, expr in rows.all()}


async def _catalog_checks(session: AsyncSession) -> set[tuple[str, str]]:
    # The conrelid filter is required: without it the catalog also returns
    # the two domain constraints of information_schema.
    rows = await session.execute(
        text(
            "SELECT c.relname, k.conname FROM pg_constraint k"
            " JOIN pg_class c ON c.oid = k.conrelid"
            " WHERE k.contype = 'c' AND c.relnamespace = 'public'::regnamespace"
            " AND c.relname IN ('users', 'products', 'alerts')"
        )
    )
    return {(table, name) for table, name in rows.all()}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", TABLES)
async def test_default_timestamps_equal_transaction_now_under_non_utc_session(
    db_session: AsyncSession, table: str
) -> None:
    await db_session.execute(text(f"SET LOCAL TimeZone = '{SESSION_ZONE}'"))
    # Positive control: the session really runs in the chosen zone.
    shown = (await db_session.execute(text("SHOW TimeZone"))).scalar_one()
    assert shown == SESSION_ZONE

    sql = {
        "users": _INSERT_USER,
        "products": _INSERT_PRODUCT,
        "alerts": _INSERT_ALERT,
    }[table]
    user_id = await _make_user(db_session) if table != "users" else 0
    product_id = await _make_product(db_session, user_id) if table == "alerts" else 0
    params: dict[str, int | None]
    if table == "users":
        params = {"tg": 700_000_002}
    elif table == "products":
        params = {"user_id": user_id, "article": 2, "current": 100, "previous": None}
    else:
        params = {"user_id": user_id, "product_id": product_id, "target": 50}
    row_id = (await db_session.execute(text(sql), params)).scalar_one()

    row = (
        await db_session.execute(
            text(
                f"SELECT created_at = now(), updated_at = now() FROM {table}"
                " WHERE id = :id"
            ),
            {"id": row_id},
        )
    ).one()
    assert tuple(row) == (True, True)


@pytest.mark.asyncio
async def test_orm_update_sets_updated_at_to_transaction_now_under_non_utc_session(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(text(f"SET LOCAL TimeZone = '{SESSION_ZONE}'"))
    # Positive control: the session really runs in the chosen zone.
    shown = (await db_session.execute(text("SHOW TimeZone"))).scalar_one()
    assert shown == SESSION_ZONE
    past = datetime(2020, 1, 1, tzinfo=UTC)
    user = User(tg_user_id=700_000_003, updated_at=past)
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    created_at = user.created_at
    assert user.updated_at == past

    user.tg_username = "renamed"
    await db_session.flush()
    await db_session.refresh(user)

    now = (await db_session.execute(text("SELECT now()"))).scalar_one()
    assert user.updated_at == now
    assert user.created_at == created_at


@pytest.mark.asyncio
async def test_catalog_defaults_of_all_six_timestamp_columns_are_now(
    db_session: AsyncSession,
) -> None:
    assert await _catalog_defaults(db_session) == dict.fromkeys(
        TIMESTAMP_COLUMNS, "now()"
    )


def test_model_server_defaults_of_all_six_timestamp_columns_are_now() -> None:
    texts: dict[tuple[str, str], str] = {}
    for table, column in TIMESTAMP_COLUMNS:
        default = Base.metadata.tables[table].c[column].server_default
        assert isinstance(default, DefaultClause)
        assert isinstance(default.arg, TextClause)
        texts[(table, column)] = default.arg.text
    assert texts == dict.fromkeys(TIMESTAMP_COLUMNS, "now()")


@pytest.mark.asyncio
async def test_catalog_check_constraints_are_exactly_the_five_checks(
    db_session: AsyncSession,
) -> None:
    assert await _catalog_checks(db_session) == EXPECTED_CHECKS


#: Model side of the same five checks: (table, name, condition text), written
#: out by hand from PROJECT.md section 6. A weakened condition under an
#: unchanged name (``> 0`` instead of ``>= 0``) must not pass.
EXPECTED_MODEL_CHECKS = {
    ("products", "ck_products_current_price_non_negative", "current_price >= 0"),
    ("products", "ck_products_previous_price_non_negative", "previous_price >= 0"),
    ("products", "ck_products_initial_price_non_negative", "initial_price >= 0"),
    ("products", "ck_products_card_price_non_negative", "card_price >= 0"),
    ("alerts", "ck_alerts_target_price_non_negative", "target_price >= 0"),
    ("users", "ck_users_product_limit_positive", "product_limit > 0"),
}


def test_model_check_constraints_are_exactly_the_five_checks() -> None:
    # Column-level checks live in ``column.constraints`` and table-level ones
    # in ``table.constraints``; both places are collected.
    found: set[tuple[str, str, str]] = set()
    for table in Base.metadata.tables.values():
        if table.name not in TABLES:
            continue
        declared = list(table.constraints)
        for column in table.columns:
            declared.extend(column.constraints)
        found |= {
            (table.name, str(constraint.name), str(constraint.sqltext))
            for constraint in declared
            if isinstance(constraint, CheckConstraint)
        }
    assert found == EXPECTED_MODEL_CHECKS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "column", "constraint"),
    [
        ("products", "current_price", "ck_products_current_price_non_negative"),
        ("products", "previous_price", "ck_products_previous_price_non_negative"),
        ("alerts", "target_price", "ck_alerts_target_price_non_negative"),
    ],
)
async def test_negative_price_is_rejected_and_zero_price_is_accepted(
    db_session: AsyncSession, table: str, column: str, constraint: str
) -> None:
    user_id = await _make_user(db_session)
    product_id = await _make_product(db_session, user_id)

    async def insert(value: int) -> int:
        if table == "alerts":
            return await _scalar_int(
                db_session,
                _INSERT_ALERT,
                user_id=user_id,
                product_id=product_id,
                target=value,
            )
        return await _scalar_int_or_null(db_session, column, value, user_id)

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await insert(-1)
    assert constraint in str(excinfo.value.orig)

    assert await insert(0) > 0


async def _scalar_int_or_null(
    session: AsyncSession, column: str, value: int, user_id: int
) -> int:
    current = value if column == "current_price" else 100
    previous = value if column == "previous_price" else None
    row_id = (
        await session.execute(
            text(_INSERT_PRODUCT),
            {
                "user_id": user_id,
                "article": 3,
                "current": current,
                "previous": previous,
            },
        )
    ).scalar_one()
    assert isinstance(row_id, int)
    return row_id


def _run_revision(connection: Connection, direction: str) -> None:
    # Built straight from the directory: no ``Config``, so the ini option
    # ``prepend_sys_path`` is never applied and ``alembic/env.py`` is not loaded.
    revision = ScriptDirectory(str(REPO_ROOT / "alembic")).get_revision(REVISION_ID)
    assert revision is not None
    module = revision.module
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


@pytest.mark.asyncio
async def test_downgrade_restores_old_schema_and_upgrade_reapplies_new_one(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    path_before = list(sys.path)

    await connection.run_sync(_run_revision, "downgrade")
    # Only the checks of later revisions (PAB-103, PAB-105, card_price) are left.
    assert await _catalog_checks(db_session) == {
        ("products", "ck_products_initial_price_non_negative"),
        ("products", "ck_products_card_price_non_negative"),
        ("users", "ck_users_product_limit_positive"),
    }
    assert await _catalog_defaults(db_session) == dict.fromkeys(
        TIMESTAMP_COLUMNS, "timezone('utc'::text, now())"
    )

    await connection.run_sync(_run_revision, "upgrade")
    assert await _catalog_checks(db_session) == EXPECTED_CHECKS
    assert await _catalog_defaults(db_session) == dict.fromkeys(
        TIMESTAMP_COLUMNS, "now()"
    )
    assert sys.path == path_before
