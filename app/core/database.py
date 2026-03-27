"""Database engine and session configuration."""

from collections.abc import AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

settings = get_settings()

async_engine = create_async_engine(
    url=settings.db.DATABASE_URL,
    echo=settings.db.ECHO,
    pool_size=settings.db.POOL_SIZE,
    max_overflow=settings.db.MAX_OVERFLOW,
    pool_pre_ping=settings.db.POOL_PRE_PING,
    pool_recycle=settings.db.POOL_RECYCLE,
)

async_session = async_sessionmaker(
    bind=async_engine,
    autoflush=settings.db.AUTOFLUSH,
    expire_on_commit=settings.db.EXPIRE_ON_COMMIT,
)


async def get_async_session() -> AsyncGenerator[AsyncSession]:
    """Provide database session with automatic rollback on error."""

    async with async_session() as session:
        try:
            yield session
        except Exception as e:
            await session.rollback()
            logger.bind(
                error_type=type(e).__name__,
                operation="db_session",
            ).exception("Database session error")
            raise
