"""Base client for any marketplace."""

from abc import ABC
from abc import abstractmethod

from app.schemas.marketplace import MarketplaceFetchResult


class BaseMarketplaceClient(ABC):
    """Base implementation for any Marketplace."""

    @abstractmethod
    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        """Get name and price for a tracked product.

        Contract (PROJECT.md section 2.6): an implementation always returns
        either `MarketplaceProductData` on success or a marked
        `MarketplaceFetchFailure` describing why the fetch did not succeed.
        Returning `None`, or letting a transport-level exception escape to
        the caller, are both contract violations.
        """
        raise NotImplementedError
