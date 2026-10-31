from collections.abc import AsyncGenerator
from functools import lru_cache
from math import ceil
from math import isfinite
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


def create_sqlite_engine(
    path: str | Path,
    *,
    echo: bool = False,
    busy_timeout_seconds: float = 30.0,
) -> AsyncEngine:
    """Create a file-based engine; the operator provisions the directory.

    sqlite_write acquires the write lock before reading to avoid stale-snapshot
    upgrades. Foreign keys, busy timeout and WAL are enabled per connection.
    """
    if not isfinite(busy_timeout_seconds) or not 0 < busy_timeout_seconds <= 300:
        raise ValueError("busy_timeout_seconds must be finite and within (0, 300]")
    timeout_ms = ceil(busy_timeout_seconds * 1000)
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(path)),
        echo=echo,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        # Explicit BEGIN ensures SAVEPOINTs are nested in real transactions,
        # including under sqlite3's legacy transaction control.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
            mode = cursor.fetchone()
            if mode is None or mode[0].lower() != "wal":
                raise RuntimeError("SQLite database could not enable WAL mode")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def begin_sqlite(connection: Connection) -> None:
        statement = (
            "BEGIN IMMEDIATE"
            if connection.get_execution_options().get("sqlite_write")
            else "BEGIN"
        )
        connection.exec_driver_sql(statement)

    return engine


@lru_cache
def get_engine() -> AsyncEngine:
    """Lazily cache the engine; disposal clears the cache.

    Safe for coroutines on one event loop, but concurrent cold calls from
    threads can create multiple engines because lru_cache does not serialize
    the wrapped function.
    """
    settings = get_settings()
    return create_sqlite_engine(
        path=settings.db.PATH,
        echo=settings.db.ECHO,
        busy_timeout_seconds=settings.db.BUSY_TIMEOUT_SECONDS,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Cache the factory bound to the engine; disposal clears both caches."""
    settings = get_settings()
    return async_sessionmaker(
        bind=get_engine(),
        autoflush=settings.db.AUTOFLUSH,
        expire_on_commit=settings.db.EXPIRE_ON_COMMIT,
    )


async def dispose_engine() -> None:
    """Clear caches before awaiting disposal to avoid reusing the old engine.

    Call after sessions have finished: disposal does not close checked-out
    connections. Shutdown waits for handlers only up to a bounded timeout.
    """
    if get_engine.cache_info().currsize:
        engine = get_engine()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
        await engine.dispose()


async def get_async_session() -> AsyncGenerator[AsyncSession]:
    """Provide database session with automatic rollback on error."""

    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.bind(
                error_type=type(e).__name__,
                operation="db_session",
            ).exception("Database session error")
            raise
