"""Create and restore consistent SQLite backups without overwriting by default."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy import inspect

from app.models import Base

ROOT = Path(__file__).resolve().parent.parent


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing file without silently creating a missing database."""
    return sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)


def _check_schema(path: Path) -> None:
    """Check integrity, references, Alembic head and the actual application schema."""
    with closing(_open_readonly(path)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("database contains invalid foreign keys")
        heads = ScriptDirectory.from_config(
            Config(str(ROOT / "alembic.ini"))
        ).get_heads()
        versions = [
            row[0] for row in db.execute("SELECT version_num FROM alembic_version")
        ]
        if sorted(versions) != sorted(heads):
            raise ValueError(
                "database migration version does not match this application"
            )

    engine = create_engine("sqlite://", creator=lambda: _open_readonly(path))
    reference = create_engine("sqlite://")
    try:
        Base.metadata.create_all(reference)
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_server_default": True}
            )
            if compare_metadata(context, Base.metadata):
                raise ValueError("database schema does not match this application")
            actual = inspect(conn)
            expected = inspect(reference)
            for table in Base.metadata.sorted_tables:
                actual_checks = actual.get_check_constraints(table.name)
                expected_checks = expected.get_check_constraints(table.name)
                if actual_checks != expected_checks:
                    raise ValueError(f"database checks do not match for {table.name}")
                if table.dialect_options["sqlite"]["autoincrement"]:
                    sql = conn.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (table.name,),
                    ).scalar_one()
                    if "AUTOINCREMENT" not in sql.upper():
                        raise ValueError(
                            f"database autoincrement missing for {table.name}"
                        )
    finally:
        engine.dispose()
        reference.dispose()


def _same_file(source: Path, destination: Path) -> bool:
    if source.resolve() == destination.resolve():
        return True
    try:
        return os.path.samefile(source, destination)
    except FileNotFoundError:
        return False


def _check_destination(destination: Path, *, overwrite: bool) -> None:
    if destination.is_symlink():
        raise ValueError("destination must not be a symbolic link")
    if destination.exists() and not overwrite:
        raise FileExistsError("destination already exists")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(destination) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError(f"database sidecar still exists: {sidecar.name}")


def _sync_file(path: Path) -> None:
    with path.open("rb") as file:
        os.fsync(file.fileno())


def _publish(source: Path, destination: Path, *, overwrite: bool) -> None:
    _sync_file(source)
    if overwrite:
        os.replace(source, destination)
    else:
        os.link(source, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def backup(source: Path, destination: str) -> None:
    """Create an online snapshot, verify it and publish without overwriting."""
    target = None if destination == "-" else Path(destination)
    if target is not None:
        if _same_file(source, target):
            raise ValueError("source and destination must differ")
        _check_destination(target, overwrite=False)
    parent = None if target is None else target.parent
    with tempfile.TemporaryDirectory(prefix="sqlite-backup-", dir=parent) as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        with (
            closing(_open_readonly(source)) as source_db,
            closing(sqlite3.connect(snapshot)) as backup_db,
        ):
            source_db.backup(backup_db)
        snapshot.chmod(0o600)
        _check_schema(snapshot)
        if target is None:
            with snapshot.open("rb") as file:
                shutil.copyfileobj(file, sys.stdout.buffer)
            sys.stdout.buffer.flush()
        else:
            _publish(snapshot, target, overwrite=False)


def restore(source: str, destination: Path, *, overwrite: bool) -> None:
    """Verify a copy before replacing a stopped application's database."""
    source_path = None if source == "-" else Path(source)
    if source_path is not None and _same_file(source_path, destination):
        raise ValueError("source and destination must differ")
    _check_destination(destination, overwrite=overwrite)
    with tempfile.TemporaryDirectory(
        prefix="sqlite-restore-", dir=destination.parent
    ) as temporary:
        snapshot = Path(temporary) / "restore.sqlite3"
        if source_path is None:
            with snapshot.open("xb") as output:
                snapshot.chmod(0o600)
                shutil.copyfileobj(sys.stdin.buffer, output)
        else:
            with (
                closing(_open_readonly(source_path)) as input_db,
                closing(sqlite3.connect(snapshot)) as output_db,
            ):
                input_db.backup(output_db)
            snapshot.chmod(0o600)
        _check_schema(snapshot)
        # Recheck immediately before publication to avoid accepting sidecars
        # created during validation. The application must remain stopped.
        _check_destination(destination, overwrite=overwrite)
        _publish(snapshot, destination, overwrite=overwrite)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup_parser = sub.add_parser("backup", help="use '-' as destination for stdout")
    backup_parser.add_argument("source", type=Path)
    backup_parser.add_argument("destination")
    restore_parser = sub.add_parser("restore", help="use '-' as source for stdin")
    restore_parser.add_argument("source")
    restore_parser.add_argument("destination", type=Path)
    restore_parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            backup(args.source, args.destination)
        else:
            restore(args.source, args.destination, overwrite=args.overwrite)
    except (OSError, sqlite3.Error, ValueError) as error:
        sys.stderr.write(f"sqlite backup: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
