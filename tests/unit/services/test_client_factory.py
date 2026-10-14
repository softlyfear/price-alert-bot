"""Tests for app.services.client_factory.get_client."""

from typing import cast

import httpx
import pytest

from app.models.enums import Marketplace
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.client_factory import get_client
from app.services.ozon_client import OzonClient
from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbAccessUnavailable
from app.services.wb_client import WbClient
from tests.unit.wb_access_stub import StubWbAccess


def test_get_client_returns_a_wildberries_client_for_wb() -> None:
    assert isinstance(
        get_client(Marketplace.wb, httpx.AsyncClient(), wb_access=StubWbAccess()),
        WbClient,
    )


def test_get_client_returns_an_ozon_client_for_ozon() -> None:
    client = get_client(Marketplace.ozon, httpx.AsyncClient(), wb_access=StubWbAccess())

    assert isinstance(client, OzonClient)
    assert not isinstance(client, WbClient)


def test_get_client_rejects_a_value_outside_the_marketplace_enum() -> None:
    """Synthetic: the `raise` branch is unreachable with the current enum, so
    it is exercised with a non-member (`cast` only silences the type)."""
    with pytest.raises(UnsupportedMarketplaceError, match="amazon"):
        get_client(
            cast(Marketplace, "amazon"), httpx.AsyncClient(), wb_access=StubWbAccess()
        )


@pytest.mark.asyncio
async def test_get_client_hands_the_given_provider_to_the_wb_client() -> None:
    """PAB-079: the provider passed to the factory is the one the WB client
    asks for access - observed through the provider's `get` calls, and the
    refusal it gives comes back as a `blocked` without a request."""
    access = StubWbAccess(WbAccessUnavailable(WbAccessStage.closed, "closed"))
    client = get_client(Marketplace.wb, httpx.AsyncClient(), wb_access=access)

    result = await client.get_product_data(1)

    assert access.get_calls == 1
    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail == "WB access unavailable: closed"


@pytest.mark.asyncio
async def test_get_client_for_ozon_never_touches_the_provider() -> None:
    """The WB provider is only for WB: an Ozon client built by the factory
    does not call it."""
    access = StubWbAccess()
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    async with httpx.AsyncClient(transport=transport) as http:
        client = get_client(Marketplace.ozon, http, wb_access=access)
        await client.get_product_data(3593896354)

    assert access.get_calls == 0
