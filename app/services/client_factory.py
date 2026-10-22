"""Client factory."""

import httpx

from app.models.enums import Marketplace
from app.services.base_client import BaseMarketplaceClient
from app.services.ozon_access import OzonAccessProvider
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonSession
from app.services.wb_access import WbAccessProvider
from app.services.wb_client import WbClient


class UnsupportedMarketplaceError(ValueError):
    """Raised when marketplace client is not implemented."""


def get_client(
    marketplace: Marketplace,
    http_client: httpx.AsyncClient,
    *,
    wb_access: WbAccessProvider,
    ozon_access: OzonAccessProvider,
    ozon_session: OzonSession,
) -> BaseMarketplaceClient:
    """Get marketplace client. The Ozon client does not use `http_client`."""

    if marketplace is Marketplace.wb:
        return WbClient(http_client, wb_access)
    if marketplace is Marketplace.ozon:
        return OzonClient(ozon_session, ozon_access)
    raise UnsupportedMarketplaceError(f"Unsupported marketplace: {marketplace}")
