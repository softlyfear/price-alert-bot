"""Client factory."""

import httpx

from app.models.enums import Marketplace
from app.services.base_client import BaseMarketplaceClient
from app.services.ozon_client import OzonClient
from app.services.wb_access import WbAccessProvider
from app.services.wb_client import WbClient


class UnsupportedMarketplaceError(ValueError):
    """Raised when marketplace client is not implemented."""


def get_client(
    marketplace: Marketplace,
    http_client: httpx.AsyncClient,
    *,
    wb_access: WbAccessProvider,
) -> BaseMarketplaceClient:
    """Get marketplace client."""

    if marketplace is Marketplace.wb:
        return WbClient(http_client, wb_access)
    if marketplace is Marketplace.ozon:
        return OzonClient(http_client)
    raise UnsupportedMarketplaceError(f"Unsupported marketplace: {marketplace}")
