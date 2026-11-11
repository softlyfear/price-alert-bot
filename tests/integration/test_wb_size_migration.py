"""Prior-revision upgrade and downgrade on isolated file-backed SQLite."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OLD = "6cd914885374"


def _migrate(
    path: Path, action: str, revision: str
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_ROOT),
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
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _seed(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    assert _migrate(path, "upgrade", _OLD).returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)
        db.execute("INSERT INTO users (id,tg_user_id) VALUES (1,100), (2,200)")
        db.execute(
            "INSERT INTO products (id,user_id,marketplace,article,product_name,"
            "current_price,previous_price,initial_price,last_checked_at,"
            "price_fetched_at) "
            "VALUES (1,1,'wb',123,'Item',1200,1300,1400,"
            "'2026-01-02 00:00:00','2026-01-01 00:00:00')"
        )
        db.execute(
            "INSERT INTO products (id,user_id,marketplace,article,product_name,"
            "current_price) VALUES (900,2,'wb',123,'Deleted',1)"
        )
        db.execute("DELETE FROM products WHERE id=900")
        db.execute(
            "INSERT INTO alerts (id,user_id,product_id,target_price,direction,"
            "triggered_at) VALUES (12,1,1,1100,'below','2026-01-01 00:00:00')"
        )
        products = db.execute("SELECT * FROM products").fetchall()
        alerts = db.execute("SELECT * FROM alerts").fetchall()
    return products, alerts


def test_upgrade_preserves_dependents_high_water_and_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite"
    products, alerts = _seed(path)
    assert _migrate(path, "upgrade", "head").returncode == 0
    assert _migrate(path, "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute(
            "SELECT wb_size_id,wb_size_name FROM products"
        ).fetchall() == [(0, None)]
        assert db.execute("SELECT * FROM alerts").fetchall() == alerts
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='products'"
        ).fetchone() == (900,)
        assert db.execute(
            "SELECT count(*) FROM pragma_index_list('products') "
            "WHERE name='ix_products_user_id'"
        ).fetchone() == (1,)
    assert _migrate(path, "downgrade", _OLD).returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("SELECT * FROM products").fetchall() == products
        assert db.execute("SELECT * FROM alerts").fetchall() == alerts
    assert _migrate(path, "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO products (user_id,marketplace,article,product_name,"
            "current_price) "
            "VALUES (2,'wb',123,'Next',100)"
        )
        assert db.execute("SELECT max(id) FROM products").fetchone() == (901,)
        db.execute("DELETE FROM products WHERE id=1")
        assert db.execute("SELECT count(*) FROM alerts").fetchone() == (0,)


def test_downgrade_refuses_selected_rows_without_changes(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite"
    _seed(path)
    assert _migrate(path, "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE products SET wb_size_id=42,wb_size_name='42' WHERE id=1")
        before = db.execute("SELECT * FROM products").fetchall()
        alerts = db.execute("SELECT * FROM alerts").fetchall()
    result = _migrate(path, "downgrade", _OLD)
    assert result.returncode != 0
    assert "Cannot downgrade while selected WB sizes exist" in result.stderr
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("SELECT * FROM products").fetchall() == before
        assert db.execute("SELECT * FROM alerts").fetchall() == alerts
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "5d2df2075e03",
        )


@pytest.mark.parametrize(
    "size,market,name",
    [(-1, "wb", None), (1, "ozon", None), (0, "wb", "wrong"), (1.5, "wb", None)],
)
def test_database_rejects_invalid_identity(
    tmp_path: Path, size: object, market: str, name: str | None
) -> None:
    path = tmp_path / "migration.sqlite"
    _seed(path)
    assert _migrate(path, "upgrade", "head").returncode == 0
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO products (user_id,marketplace,article,product_name,"
                "current_price,wb_size_id,wb_size_name) "
                "VALUES (1,?,333,'Item',100,?,?)",
                (market, size, name),
            )
