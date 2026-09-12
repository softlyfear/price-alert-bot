"""Task scheduler."""

from sqlalchemy.ext.asyncio import AsyncSession


async def price_price_check_loop(session: AsyncSession) -> None:
    """Update price for all active products."""
    raise NotImplementedError
