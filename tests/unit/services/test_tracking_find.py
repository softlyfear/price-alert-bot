"""Unit tests for ``TrackingService.find_tracked_product`` (PAB-096).

The boundary mocked is the repository layer (as in ``test_tracking_list``);
the SQL predicate on the marketplace and the statement count are proven on a
real PostgreSQL in ``tests/integration/services/test_tracking_find_live.py``.
Here: the lookup arguments, the sorting of the card and the "no product" exit.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.tracking as tracking_module
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.services.tracking import ProductCard
from app.services.tracking import TrackingService

_TG = 555
_USER_ID = 77  # deliberately different from the Telegram id
_ARTICLE = 3_593_896_354


def _product() -> Product:
    return Product(
        id=10,
        user_id=_USER_ID,
        marketplace=Marketplace.ozon,
        article=_ARTICLE,
        product_name="P",
        current_price=100,
    )


def _alert(alert_id: int, target: int) -> Alert:
    return Alert(
        id=alert_id,
        user_id=_USER_ID,
        product_id=10,
        target_price=target,
        direction=AlertDirection.below,
    )


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.users = MagicMock(spec=UserRepository)
        self.users.get_or_create_by_tg_id.return_value = (
            User(id=_USER_ID, tg_user_id=_TG),
            False,
        )
        self.users.get_by_tg_id.return_value = (
            self.users.get_or_create_by_tg_id.return_value[0]
        )
        self.products = MagicMock(spec=ProductRepository)
        self.alerts = MagicMock(spec=AlertRepository)
        self.client_factory = MagicMock()
        monkeypatch.setattr(tracking_module, "UserRepository", lambda _s: self.users)
        monkeypatch.setattr(
            tracking_module, "ProductRepository", lambda _s: self.products
        )
        monkeypatch.setattr(tracking_module, "AlertRepository", lambda _s: self.alerts)
        self.service = TrackingService(
            session=cast(AsyncSession, MagicMock(spec=AsyncSession)),
            http_client=httpx.AsyncClient(),
            client_factory=self.client_factory,
            max_products_per_user=50,
        )


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


@pytest.mark.asyncio
async def test_find_returns_the_card_with_alerts_sorted_by_target_price(
    h: _Harness,
) -> None:
    product = _product()
    h.products.get_by_article_and_user.return_value = product
    h.alerts.get_by_user_and_product.return_value = [
        _alert(3, 900),
        _alert(1, 100),
        _alert(2, 500),
    ]

    card = await h.service.find_tracked_product(_TG, Marketplace.ozon, _ARTICLE)

    assert isinstance(card, ProductCard)
    assert card.product is product
    assert isinstance(card.alerts, tuple)
    assert [a.target_price for a in card.alerts] == [100, 500, 900]


@pytest.mark.asyncio
async def test_find_scopes_the_lookup_to_the_internal_user_id_and_the_marketplace(
    h: _Harness,
) -> None:
    h.products.get_by_article_and_user.return_value = _product()
    h.alerts.get_by_user_and_product.return_value = []

    await h.service.find_tracked_product(_TG, Marketplace.ozon, _ARTICLE)

    h.users.get_by_tg_id.assert_awaited_once_with(_TG)
    h.products.get_by_article_and_user.assert_awaited_once_with(
        _ARTICLE, _USER_ID, Marketplace.ozon
    )
    h.alerts.get_by_user_and_product.assert_awaited_once_with(_USER_ID, 10)
    h.client_factory.assert_not_called()


@pytest.mark.asyncio
async def test_find_without_a_product_is_none_and_skips_the_alert_query(
    h: _Harness,
) -> None:
    h.products.get_by_article_and_user.return_value = None

    assert await h.service.find_tracked_product(_TG, Marketplace.wb, _ARTICLE) is None

    h.alerts.get_by_user_and_product.assert_not_called()
    h.client_factory.assert_not_called()
