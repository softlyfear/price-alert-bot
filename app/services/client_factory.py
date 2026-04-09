"""Client factory."""

import httpx

from app.models.enums import Marketplace
from app.services.base_client import BaseMarketplaceClient
from app.services.wb_client import WbClient


class UnsupportedMarketplaceError(ValueError):
    pass


def get_client(
    marketplace: Marketplace,
    http_client: httpx.AsyncClient,
) -> BaseMarketplaceClient:
    """Get marketplace client."""
    if marketplace is Marketplace.wb:
        return WbClient(http_client)
