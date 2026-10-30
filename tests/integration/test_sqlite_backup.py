"""Backup and restore use consistent snapshots without damaging destination data."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from scripts.sqlite_backup import backup
from scripts.sqlite_backup import restore
from tests.integration.conftest import REPO_ROOT


def _seed(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(tg_user_id) VALUES (123)")
        db.execute(
            "INSERT INTO products(user_id,marketplace,article,product_name,"
            "current_price,price_fetched_at) VALUES "
            "(1,'wb',456,'test',100,'2026-10-01 12:30:00.000000')"
        )
        db.execute(
            "INSERT INTO alerts(user_id,product_id,target_price,direction) "
            "VALUES (1,1,50,'below')"
        )
        db.commit()


def test_online_backup_and_restore_roundtrip(
    database_path: Path, tmp_path: Path
) -> None:
    _seed(database_path)
    snapshot = tmp_path / "backup.sqlite3"
    destination = tmp_path / "restored.sqlite3"
    with closing(sqlite3.connect(database_path)) as live:
        live.execute("PRAGMA journal_mode=WAL")
        backup(database_path, str(snapshot))
        assert live.execute("SELECT count(*) FROM products").fetchone() == (1,)
    assert snapshot.stat().st_mode & 0o777 == 0o600
    restore(str(snapshot), destination, overwrite=False)
    assert destination.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(destination)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT article,current_price,price_fetched_at FROM products"
        ).fetchone() == (456, 100, "2026-10-01 12:30:00.000000")
        assert db.execute("SELECT count(*) FROM alerts").fetchone() == (1,)


def test_missing_source_never_created_and_backup_never_overwrites(
    database_path: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.sqlite3"
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"existing backup")
    with pytest.raises(FileNotFoundError):
        backup(missing, str(tmp_path / "out.sqlite3"))
    assert not missing.exists()
    with pytest.raises(FileExistsError):
        backup(database_path, str(existing))
    assert existing.read_bytes() == b"existing backup"


def test_restore_validates_before_replacing_destination(
    database_path: Path, tmp_path: Path
) -> None:
    _seed(database_path)
    original = database_path.read_bytes()
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        restore(str(corrupt), database_path, overwrite=True)
    assert database_path.read_bytes() == original
    with pytest.raises(FileExistsError):
        restore(str(corrupt), database_path, overwrite=False)
    assert database_path.read_bytes() == original


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_restore_refuses_stale_sidecars(
    database_path: Path, tmp_path: Path, suffix: str
) -> None:
    snapshot = tmp_path / "snapshot.sqlite3"
    backup(database_path, str(snapshot))
    original = database_path.read_bytes()
    sidecar = Path(str(database_path) + suffix)
    sidecar.write_text("stale")
    with pytest.raises(ValueError, match="sidecar"):
        restore(str(snapshot), database_path, overwrite=True)
    assert database_path.read_bytes() == original


@pytest.mark.parametrize("alias", ["same", "hardlink", "symlink"])
def test_backup_rejects_aliases(
    database_path: Path, tmp_path: Path, alias: str
) -> None:
    target = tmp_path / "alias.sqlite3"
    if alias == "same":
        target = database_path
    elif alias == "hardlink":
        os.link(database_path, target)
    else:
        target.symlink_to(database_path)
    original = database_path.read_bytes()
    with pytest.raises(ValueError):
        backup(database_path, str(target))
    assert database_path.read_bytes() == original


def test_cli_stdout_stdin_backup_restore(database_path: Path, tmp_path: Path) -> None:
    _seed(database_path)
    backup_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.sqlite_backup",
            "backup",
            str(database_path),
            "-",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert backup_result.returncode == 0, backup_result.stderr
    assert backup_result.stdout.startswith(b"SQLite format 3\x00")
    destination = tmp_path / "restore.sqlite3"
    restored = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.sqlite_backup",
            "restore",
            "-",
            str(destination),
        ],
        cwd=REPO_ROOT,
        input=backup_result.stdout,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert restored.returncode == 0, restored.stderr
    with closing(sqlite3.connect(destination)) as db:
        assert db.execute("SELECT tg_user_id FROM users").fetchone() == (123,)


def test_restore_rejects_missing_index_without_changing_destination(
    database_path: Path, tmp_path: Path
) -> None:
    _seed(database_path)
    destination = tmp_path / "existing.sqlite3"
    backup(database_path, str(destination))
    original = destination.read_bytes()
    with closing(sqlite3.connect(database_path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("DROP INDEX ix_products_user_id")
        db.commit()
        with pytest.raises(ValueError):
            restore(str(database_path), destination, overwrite=True)
        assert destination.read_bytes() == original


def test_restore_includes_committed_data_from_open_source_wal(
    database_path: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "restored.sqlite3"
    with closing(sqlite3.connect(database_path)) as source:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        source.execute("INSERT INTO users(tg_user_id) VALUES (987654321)")
        source.execute(
            "INSERT INTO products(user_id,marketplace,article,product_name,"
            "current_price) VALUES (1,'ozon',123456789,'WAL product',5700)"
        )
        source.commit()
        restore(str(database_path), destination, overwrite=False)
        with closing(sqlite3.connect(destination)) as restored:
            assert restored.execute("SELECT tg_user_id FROM users").fetchone() == (
                987654321,
            )
            assert restored.execute(
                "SELECT article, product_name, current_price FROM products"
            ).fetchone() == (123456789, "WAL product", 5700)
