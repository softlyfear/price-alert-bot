"""Persisted product availability status on isolated file-backed SQLite (PAB-135)."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PREVIOUS = "5d2df2075e03"
_REASONS = ("not_found", "blocked", "transport_error", "bad_payload", "out_of_stock")


def _migrate(
    path: Path, action: str, revision: str
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_REPO_ROOT),
        "DB__PATH": str(path),
        "TG__BOT_TOKEN": "test-suite-placeholder-token",
        "REDIS__HOST": "localhost",
    }
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.core.config import Settings; "
            'Settings.model_config["env_file"] = None; '
            "from alembic.config import main; main()",
            action,
            revision,
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _seed(path: Path) -> tuple[dict[str, object], list[tuple[object, ...]]]:
    assert _migrate(path, "upgrade", _PREVIOUS).returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users (id,tg_user_id) VALUES (1,100)")
        db.execute(
            "INSERT INTO products (id,user_id,marketplace,article,product_name,"
            "current_price,previous_price,initial_price,last_checked_at,"
            "price_fetched_at) "
            "VALUES (1,1,'wb',123,'Item',1200,1300,1400,"
            "'2026-01-02 00:00:00','2026-01-01 00:00:00')"
        )
        db.execute(
            "INSERT INTO alerts (id,user_id,product_id,target_price,direction,"
            "triggered_at) VALUES (12,1,1,1100,'below','2026-01-01 00:00:00')"
        )
        db.row_factory = sqlite3.Row
        try:
            product = dict(db.execute("SELECT * FROM products").fetchone())
        finally:
            db.row_factory = None
        alerts = db.execute("SELECT * FROM alerts").fetchall()
    return product, alerts


def _product_row(db: sqlite3.Connection) -> dict[str, object]:
    db.row_factory = sqlite3.Row
    try:
        return dict(db.execute("SELECT * FROM products").fetchone())
    finally:
        db.row_factory = None


def test_upgrade_keeps_rows_and_leaves_the_status_empty(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite"
    product, alerts = _seed(path)
    assert _migrate(path, "upgrade", "head").returncode == 0
    assert _migrate(path, "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        upgraded = _product_row(db)
        assert upgraded.pop("last_failure") is None
        assert upgraded == product
        assert db.execute("SELECT * FROM alerts").fetchall() == alerts
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' "
            "AND name='ix_products_user_id'"
        ).fetchone() == (1,)


def test_downgrade_drops_the_status_and_preserves_alerts(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite"
    product, alerts = _seed(path)
    assert _migrate(path, "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE products SET last_failure='out_of_stock' WHERE id=1")
    assert _migrate(path, "downgrade", _PREVIOUS).returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        assert _product_row(db) == product
        assert db.execute("SELECT * FROM alerts").fetchall() == alerts
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _PREVIOUS,
        )


@pytest.mark.parametrize("reason", _REASONS)
def test_every_persisted_status_is_accepted(tmp_path: Path, reason: str) -> None:
    path = tmp_path / "migration.sqlite"
    _seed(path)
    assert _migrate(path, "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE products SET last_failure=? WHERE id=1", (reason,))
        assert db.execute("SELECT last_failure FROM products").fetchone() == (reason,)
        db.execute("UPDATE products SET last_failure=NULL WHERE id=1")
        assert db.execute("SELECT last_failure FROM products").fetchone() == (None,)


@pytest.mark.parametrize("value", ["", "unknown", "OUT_OF_STOCK", "not found", 1])
def test_an_unknown_status_is_rejected(tmp_path: Path, value: object) -> None:
    path = tmp_path / "migration.sqlite"
    _seed(path)
    assert _migrate(path, "upgrade", "head").returncode == 0
    with (
        closing(sqlite3.connect(path)) as db,
        db,
        pytest.raises(sqlite3.IntegrityError),
    ):
        db.execute("UPDATE products SET last_failure=? WHERE id=1", (value,))
