"""Database engine and session configuration."""

from collections.abc import AsyncGenerator
from functools import lru_cache

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Build (or return the cached) process-wide async engine.

    Lazy on purpose: reading settings and building an engine at import time
    means the module cannot even be imported without a fully configured
    environment. Caching is done with ``functools.lru_cache`` on this
    zero-argument function, so every call after the first returns the exact
    same object. ``dispose_engine()`` invalidates the cache via
    ``get_engine.cache_clear()`` -- without that, a disposed engine would
    stay cached and be handed out again.

    Not a concurrency hazard: the whole body runs without ever awaiting, so
    there is no point between the cache check and the cache write where
    another coroutine can run. Two concurrent first callers cannot race into
    building two engines.
    """
    settings = get_settings()
    return create_async_engine(
        url=settings.db.DATABASE_URL,
        echo=settings.db.ECHO,
        pool_size=settings.db.POOL_SIZE,
        max_overflow=settings.db.MAX_OVERFLOW,
        pool_pre_ping=settings.db.POOL_PRE_PING,
        pool_recycle=settings.db.POOL_RECYCLE,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Build (or return the cached) process-wide session factory.

    Cached the same way as ``get_engine`` and for the same reason. Bound to
    whatever engine ``get_engine()`` currently returns, so ``dispose_engine``
    clears this cache too -- otherwise the factory would keep handing out
    sessions bound to an already-disposed engine.
    """
    settings = get_settings()
    return async_sessionmaker(
        bind=get_engine(),
        autoflush=settings.db.AUTOFLUSH,
        expire_on_commit=settings.db.EXPIRE_ON_COMMIT,
    )


async def dispose_engine() -> None:
    """Dispose the engine's connection pool and reset both caches.

    Resetting the caches is mandatory: without it, the next call to
    ``get_engine``/``get_sessionmaker`` would return the already-disposed
    objects instead of building fresh ones. Safe to call when no engine was
    ever built -- ``cache_info().currsize`` distinguishes that case, so this
    does not build an engine just to dispose it.
    """
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


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
