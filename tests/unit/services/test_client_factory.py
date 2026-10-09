"""Tests for app.services.client_factory.get_client."""

from typing import cast

import httpx
import pytest

from app.models.enums import Marketplace
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.client_factory import get_client
from app.services.ozon_client import OzonClient
from app.services.wb_client import WbClient


def test_get_client_returns_a_wildberries_client_for_wb() -> None:
    assert isinstance(get_client(Marketplace.wb, httpx.AsyncClient()), WbClient)


def test_get_client_returns_an_ozon_client_for_ozon() -> None:
    client = get_client(Marketplace.ozon, httpx.AsyncClient())

    assert isinstance(client, OzonClient)
    assert not isinstance(client, WbClient)


def test_get_client_rejects_a_value_outside_the_marketplace_enum() -> None:
    """Synthetic: the `raise` branch is unreachable with the current enum, so
    it is exercised with a non-member (`cast` only silences the type)."""
    with pytest.raises(UnsupportedMarketplaceError, match="amazon"):
        get_client(cast(Marketplace, "amazon"), httpx.AsyncClient())
