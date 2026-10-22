"""Tests for app.services.client_factory.get_client."""

from typing import cast

import httpx
import pytest

from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.client_factory import get_client
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_access import OzonAccessUnavailable
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonReply
from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbAccessUnavailable
from app.services.wb_client import WbClient
from tests.unit.ozon_stubs import FakeOzonSession
from tests.unit.ozon_stubs import StubOzonAccess
from tests.unit.wb_access_stub import StubWbAccess


def _build(
    marketplace: Marketplace,
    http: httpx.AsyncClient | None = None,
    *,
    wb_access: StubWbAccess | None = None,
    ozon_access: StubOzonAccess | None = None,
    session: FakeOzonSession | None = None,
) -> object:
    return get_client(
        marketplace,
        http or httpx.AsyncClient(),
        wb_access=wb_access or StubWbAccess(),
        ozon_access=ozon_access or StubOzonAccess(),
        ozon_session=session or FakeOzonSession(OzonReply(200, b"")),
    )


def test_get_client_returns_a_wildberries_client_for_wb() -> None:
    assert isinstance(_build(Marketplace.wb), WbClient)


def test_get_client_returns_an_ozon_client_for_ozon() -> None:
    client = _build(Marketplace.ozon)

    assert isinstance(client, OzonClient)
    assert not isinstance(client, WbClient)


def test_get_client_rejects_a_value_outside_the_marketplace_enum() -> None:
    """Synthetic: the `raise` branch is unreachable with the current enum, so
    it is exercised with a non-member (`cast` only silences the type)."""
    with pytest.raises(UnsupportedMarketplaceError, match="amazon"):
        _build(cast(Marketplace, "amazon"))


@pytest.mark.asyncio
async def test_get_client_hands_the_given_provider_to_the_wb_client() -> None:
    """PAB-079: the provider passed to the factory is the one the WB client
    asks for access - observed through the provider's `get` calls, and the
    refusal it gives comes back as a `blocked` without a request."""
    access = StubWbAccess(WbAccessUnavailable(WbAccessStage.closed, "closed"))
    ozon = StubOzonAccess()
    client = cast(WbClient, _build(Marketplace.wb, wb_access=access, ozon_access=ozon))

    result = await client.get_product_data(1)

    assert access.get_calls == 1
    assert ozon.get_calls == 0
    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail == "WB access unavailable: closed"


@pytest.mark.asyncio
async def test_get_client_hands_the_ozon_provider_and_session_to_the_ozon_client() -> (
    None
):
    """PAB-110: an Ozon client built by the factory asks the given Ozon
    provider (never the WB one) and sends through the given session."""
    wb = StubWbAccess()
    ozon = StubOzonAccess()
    session = FakeOzonSession(OzonReply(404, None))
    client = cast(
        OzonClient,
        _build(Marketplace.ozon, wb_access=wb, ozon_access=ozon, session=session),
    )

    result = await client.get_product_data(3593896354)

    assert wb.get_calls == 0
    assert ozon.get_calls == 1
    assert len(session.requests) == 1
    assert session.requests[0][2] == ozon.issued[0]
    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.not_found, detail="HTTP 404"
    )


@pytest.mark.asyncio
async def test_ozon_refusal_through_the_factory_is_blocked_without_request() -> None:
    ozon = StubOzonAccess(OzonAccessUnavailable(OzonAccessStage.paused, "pause"))
    session = FakeOzonSession(OzonReply(200, b""))
    client = cast(
        OzonClient, _build(Marketplace.ozon, ozon_access=ozon, session=session)
    )

    result = await client.get_product_data(1)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.blocked
    assert session.requests == []


@pytest.mark.asyncio
async def test_ozon_client_does_not_use_the_shared_http_client() -> None:
    """The Ozon client goes through `OzonSession`: a shared `httpx` client whose
    transport fails the test is never called."""

    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the Ozon client must not use the httpx client")

    session = FakeOzonSession(OzonReply(404, None))
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        client = cast(OzonClient, _build(Marketplace.ozon, http, session=session))
        await client.get_product_data(1)

    assert len(session.requests) == 1
