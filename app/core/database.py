"""Database engine and session configuration."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core import settings

async_engine = create_async_engine(
    settings.bd.url,
    echo=settings.bd.echo,
)


async_session = async_sessionmaker(
    bind=async_engine,
    autoflush=settings.bd.autoflush,
    expire_on_commit=settings.bd.expire_on_commit,
)


async def get_async_session() -> AsyncGenerator[AsyncSession]:
    """Provide database session with automatic rollback on error."""

    async with async_session() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
