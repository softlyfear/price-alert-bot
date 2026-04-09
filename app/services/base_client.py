"""Base client for any marketplace."""

from abc import ABC
from abc import abstractmethod

from app.schemas.marketplace import MarketplaceProductData


class BaseMarketplaceClient(ABC):
    """Base implementation for any Marketplace."""

    @abstractmethod
    async def get_product_data(self, article: int) -> MarketplaceProductData | None:
        """Get name and price saved product."""
        raise NotImplementedError
