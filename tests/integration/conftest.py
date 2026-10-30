"""Isolated file-backed SQLite fixtures built from the real migrations."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_sqlite_engine
from app.repositories.user import UserRepository

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Migrate once and copy the resulting empty database for each test."""
    path = tmp_path_factory.mktemp("sqlite-template") / "template.sqlite3"
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
            "upgrade",
            "head",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Alembic failed: {result.stdout}\n{result.stderr}")
    return path


@pytest.fixture
def database_path(tmp_path: Path, migrated_template: Path) -> Path:
    """Copy a consistent schema snapshot to a test-owned file."""
    path = tmp_path / "test.sqlite3"
    with (
        closing(sqlite3.connect(migrated_template)) as source,
        closing(sqlite3.connect(path)) as dest,
    ):
        source.backup(dest)
    return path


@pytest.fixture
def database_url(database_path: Path) -> str:
    return f"sqlite+aiosqlite:///{database_path}"


@pytest_asyncio.fixture
async def db_session(database_path: Path) -> AsyncIterator[AsyncSession]:
    """Provide a real session; the database file is isolated per test."""
    engine = create_sqlite_engine(database_path)
    try:
        async with AsyncSession(
            engine, autoflush=False, expire_on_commit=False
        ) as session:
            await UserRepository(session).begin_write()
            yield session
    finally:
        await engine.dispose()
