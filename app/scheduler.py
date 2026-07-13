"""Task scheduler."""

from sqlalchemy.ext.asyncio import AsyncSession


async def price_price_check_loop(session: AsyncSession):
    """Update price for all active products."""
    while True:
        pass
