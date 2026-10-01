"""Tests for app.services.client_factory.get_client."""

import httpx
import pytest

from app.models.enums import Marketplace
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.client_factory import get_client
from app.services.wb_client import WbClient


def test_get_client_returns_a_wildberries_client_for_wb() -> None:
    assert isinstance(get_client(Marketplace.wb, httpx.AsyncClient()), WbClient)


def test_get_client_rejects_ozon_until_it_is_implemented() -> None:
    with pytest.raises(UnsupportedMarketplaceError):
        get_client(Marketplace.ozon, httpx.AsyncClient())
