"""SQLite migration, constraints and UTC behaviour on test-owned files."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import create_sqlite_engine
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from tests.integration.conftest import REPO_ROOT


def _migrate(path: Path, *args: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "DB__PATH": str(path),
        "TG__BOT_TOKEN": "test-suite-placeholder-token",
        "REDIS__HOST": "localhost",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.core.config import Settings; "
            'Settings.model_config["env_file"] = None; '
            "from alembic.config import main; main()",
            *args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_empty_migration_repeat_downgrade_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "with space %25.sqlite3"
    _migrate(path, "upgrade", "head")
    _migrate(path, "upgrade", "head")
    with closing(sqlite3.connect(path)) as db:
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"users", "products", "alerts", "alembic_version"} <= tables
    _migrate(path, "downgrade", "base")
    with closing(sqlite3.connect(path)) as db:
        tables = {
            r[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not ({"users", "products", "alerts"} & tables)
    _migrate(path, "upgrade", "head")
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone() == (0,)


def test_constraints_cascade_and_ids_not_reused(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        user = db.execute(
            "INSERT INTO users(tg_user_id) VALUES (?)", (2**40,)
        ).lastrowid
        assert user is not None
        insert = (
            "INSERT INTO products(user_id,marketplace,article,product_name,"
            "current_price) VALUES (?,?,?,?,?)"
        )
        product = db.execute(insert, (user, "wb", 2**40, "test", 100)).lastrowid
        assert product is not None
        db.execute(
            "INSERT INTO alerts(user_id,product_id,target_price,direction) "
            "VALUES (?,?,?,?)",
            (user, product, 50, "below"),
        )
        for values in (
            (user, "invalid", 12, "x", 100),
            (user, "wb", 12, "x", -1),
            (999999, "wb", 12, "x", 100),
            (user, "wb", 2**40, "x", 100),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(insert, values)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO alerts(user_id,product_id,target_price,direction) "
                "VALUES (?,?,?,?)",
                (user, product, 50, "other"),
            )
        db.execute("DELETE FROM products WHERE id=?", (product,))
        assert db.execute("SELECT count(*) FROM alerts").fetchone() == (0,)
        new_product = db.execute(insert, (user, "ozon", 22, "new", 100)).lastrowid
        assert new_product is not None and new_product > product
        db.execute("DELETE FROM users WHERE id=?", (user,))
        assert db.execute("SELECT count(*) FROM products").fetchone() == (0,)
        new_user = db.execute("INSERT INTO users(tg_user_id) VALUES (42)").lastrowid
        assert new_user is not None and new_user > user


@pytest.mark.asyncio
async def test_aware_timestamps_survive_reopening_and_naive_rejected(
    database_path: Path,
) -> None:
    engine = create_sqlite_engine(database_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    shifted = datetime(2026, 10, 1, 15, 30, tzinfo=timezone(timedelta(hours=3)))
    try:
        async with factory() as session:
            user = User(tg_user_id=777)
            session.add(user)
            await session.flush()
            product = Product(
                user_id=user.id,
                marketplace=Marketplace.wb,
                article=42,
                product_name="x",
                current_price=100,
                price_fetched_at=shifted,
            )
            session.add(product)
            await session.flush()
            alert = Alert(
                user_id=user.id,
                product_id=product.id,
                target_price=50,
                direction=AlertDirection.below,
                triggered_at=shifted,
            )
            session.add(alert)
            await session.commit()
        await engine.dispose()
        async with factory() as session:
            product = (await session.scalars(select(Product))).one()
            alert = (await session.scalars(select(Alert))).one()
            assert product.price_fetched_at == datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
            assert alert.triggered_at == product.price_fetched_at
            assert product.created_at.tzinfo is UTC
            assert product.updated_at.tzinfo is UTC
            assert product.last_checked_at is None
            product.price_fetched_at = datetime(2026, 1, 1)  # noqa: DTZ001 -- deliberately invalid
            with pytest.raises(StatementError, match="timezone-aware"):
                await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_connection_requires_real_wal_support() -> None:
    engine = create_sqlite_engine(":memory:")
    try:
        with pytest.raises(RuntimeError, match="enable WAL"):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()


def test_utc_datetime_normalizes_an_aware_driver_result() -> None:
    from sqlalchemy.dialects.sqlite import dialect

    from app.models.base import UTCDateTime

    value = datetime(2026, 10, 1, 15, 30, tzinfo=timezone(timedelta(hours=3)))
    assert UTCDateTime().process_result_value(value, dialect()) == datetime(
        2026, 10, 1, 12, 30, tzinfo=UTC
    )
