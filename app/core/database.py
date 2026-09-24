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

    Not a concurrency hazard for coroutines sharing one event loop: the whole
    body runs without ever awaiting, so there is no point between the cache
    check and the cache write where another coroutine can run. Two
    concurrent first callers on the same loop cannot race into building two
    engines.

    This guarantee does not extend to threads. ``lru_cache`` calls the
    wrapped function outside of its internal lock, so two threads racing
    into a cold cache can both execute this body concurrently; only one of
    the two resulting engines ends up cached, and the other -- with its own
    connection pool -- is returned to nobody and never closed by
    ``dispose_engine()``. FastAPI runs synchronous dependencies and
    synchronous handlers in a threadpool, so this matters the moment a
    synchronous call path reaches ``get_engine()``.
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

    Both caches are cleared *before* the ``await`` on ``dispose()``, not
    after. ``AsyncEngine.dispose()`` does not put the engine into a
    terminal state -- it replaces its connection pool with a fresh one and
    leaves the engine object perfectly usable. If the caches were cleared
    only after awaiting, a coroutine calling ``get_async_session()`` during
    that window would still see the not-yet-cleared factory bound to the
    same engine object, and would silently open connections on the new pool
    after shutdown has already begun; once the caches are finally cleared,
    no reference to that pool remains, so a later ``dispose()`` cannot close
    it either. Grabbing the engine reference and clearing both caches
    synchronously, before the only point of control transfer in this
    function, closes that window: any caller reaching this point after the
    clear builds a brand new engine instead of reaching the one about to be
    disposed. Safe to call when no engine was ever built --
    ``cache_info().currsize`` distinguishes that case, so this does not
    build an engine just to dispose it.

    Precondition owned by the caller, not enforced here: nothing should
    still be using a session bound to this engine when this is called.
    ``AsyncEngine.dispose()`` only closes connections currently checked
    back into the pool; a connection checked out by an in-flight session
    is left alone and only closed later, on garbage collection of the
    now-orphaned pool (SQLAlchemy docs, "Engine Disposal") -- so calling
    this while a session is still in use does not raise, but provides no
    synchronization with that session either. ``app.main``'s shutdown
    sequence (PROJECT.md §8.3, Р6) satisfies this by calling
    ``dispose_engine()`` last, after waiting for in-flight Telegram updates
    up to a bounded timeout. That wait can itself time out, so on a slow
    handler this precondition is only best-effort, not guaranteed.
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
