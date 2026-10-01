"""Unit tests for the list/card/removal methods of TrackingService (PAB-070).

The boundary mocked is the repository layer (as in ``test_tracking_service``);
SQL, ownership predicates and cascades are proven on a real PostgreSQL in
``tests/integration/services/test_tracking_list_live.py``. Here: ordering,
the scoping arguments, the removal decision tree and the lost-race branch.
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
from app.services.tracking import AlertRemoval
from app.services.tracking import ProductCard
from app.services.tracking import TrackingService

_TG = 555
_USER_ID = 77  # deliberately different from the Telegram id


def _product(product_id: int) -> Product:
    return Product(
        id=product_id,
        user_id=_USER_ID,
        marketplace=Marketplace.wb,
        article=product_id,
        product_name="P",
        current_price=100,
    )


def _alert(alert_id: int, target: int, product_id: int = 10) -> Alert:
    return Alert(
        id=alert_id,
        user_id=_USER_ID,
        product_id=product_id,
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
        self.products = MagicMock(spec=ProductRepository)
        self.alerts = MagicMock(spec=AlertRepository)
        monkeypatch.setattr(tracking_module, "UserRepository", lambda _s: self.users)
        monkeypatch.setattr(
            tracking_module, "ProductRepository", lambda _s: self.products
        )
        monkeypatch.setattr(tracking_module, "AlertRepository", lambda _s: self.alerts)
        self.service = TrackingService(
            session=cast(AsyncSession, MagicMock(spec=AsyncSession)),
            http_client=httpx.AsyncClient(),
            client_factory=MagicMock(),
            max_products_per_user=50,
        )


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


# --- list_products -------------------------------------------------------


@pytest.mark.asyncio
async def test_list_products_sorts_by_id_ascending_whatever_the_repo_order(
    h: _Harness,
) -> None:
    h.products.get_by_user_id.return_value = [_product(30), _product(5), _product(12)]

    result = await h.service.list_products(_TG)

    assert [p.id for p in result] == [5, 12, 30]


@pytest.mark.asyncio
async def test_list_products_scopes_to_the_internal_user_id_not_the_telegram_id(
    h: _Harness,
) -> None:
    h.products.get_by_user_id.return_value = []

    assert await h.service.list_products(_TG) == []

    h.users.get_or_create_by_tg_id.assert_awaited_once_with(_TG)
    h.products.get_by_user_id.assert_awaited_once_with(_USER_ID)


# --- get_product_card ----------------------------------------------------


@pytest.mark.asyncio
async def test_get_product_card_returns_product_and_alerts_sorted_by_target(
    h: _Harness,
) -> None:
    product = _product(10)
    h.products.get_by_id_for_user.return_value = product
    h.alerts.get_by_user_and_product.return_value = [
        _alert(3, 900),
        _alert(1, 100),
        _alert(2, 500),
    ]

    card = await h.service.get_product_card(_TG, 10)

    assert isinstance(card, ProductCard)
    assert card.product is product
    assert isinstance(card.alerts, tuple)
    assert [a.target_price for a in card.alerts] == [100, 500, 900]
    h.products.get_by_id_for_user.assert_awaited_once_with(10, _USER_ID)
    h.alerts.get_by_user_and_product.assert_awaited_once_with(_USER_ID, 10)


@pytest.mark.asyncio
async def test_get_product_card_for_unowned_or_missing_product_is_none_and_skips_alerts(
    h: _Harness,
) -> None:
    h.products.get_by_id_for_user.return_value = None

    assert await h.service.get_product_card(_TG, 10) is None

    h.alerts.get_by_user_and_product.assert_not_called()


@pytest.mark.asyncio
async def test_get_product_card_without_alerts_has_empty_tuple(h: _Harness) -> None:
    h.products.get_by_id_for_user.return_value = _product(10)
    h.alerts.get_by_user_and_product.return_value = []

    card = await h.service.get_product_card(_TG, 10)

    assert card is not None
    assert card.alerts == ()


# --- remove_product ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [True, False])
async def test_remove_product_returns_the_repository_verdict_scoped_to_user(
    h: _Harness, deleted: bool
) -> None:
    h.products.delete_for_user.return_value = deleted

    assert await h.service.remove_product(_TG, 10) is deleted

    h.products.delete_for_user.assert_awaited_once_with(10, _USER_ID)


# --- remove_alert --------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_alert_not_owned_is_not_found_and_deletes_nothing(
    h: _Harness,
) -> None:
    h.alerts.get_by_id_for_user.return_value = None

    assert await h.service.remove_alert(_TG, 1) is AlertRemoval.not_found

    h.alerts.get_by_id_for_user.assert_awaited_once_with(1, _USER_ID)
    h.alerts.delete_for_user.assert_not_called()
    h.products.delete_for_user.assert_not_called()


@pytest.mark.asyncio
async def test_remove_alert_lost_race_is_not_found_and_keeps_the_product(
    h: _Harness,
) -> None:
    """The row vanished between the lookup and the DELETE (rowcount 0)."""
    h.alerts.get_by_id_for_user.return_value = _alert(1, 100)
    h.alerts.delete_for_user.return_value = False

    assert await h.service.remove_alert(_TG, 1) is AlertRemoval.not_found

    h.alerts.get_by_user_and_product.assert_not_called()
    h.products.delete_for_user.assert_not_called()


@pytest.mark.asyncio
async def test_remove_alert_with_siblings_left_is_removed_and_keeps_the_product(
    h: _Harness,
) -> None:
    h.alerts.get_by_id_for_user.return_value = _alert(1, 100, product_id=10)
    h.alerts.delete_for_user.return_value = True
    h.alerts.get_by_user_and_product.return_value = [_alert(2, 200, product_id=10)]

    assert await h.service.remove_alert(_TG, 1) is AlertRemoval.removed

    h.alerts.delete_for_user.assert_awaited_once_with(1, _USER_ID)
    h.alerts.get_by_user_and_product.assert_awaited_once_with(_USER_ID, 10)
    h.products.delete_for_user.assert_not_called()


@pytest.mark.asyncio
async def test_remove_alert_last_one_drops_the_product_of_that_alert(
    h: _Harness,
) -> None:
    h.alerts.get_by_id_for_user.return_value = _alert(1, 100, product_id=10)
    h.alerts.delete_for_user.return_value = True
    h.alerts.get_by_user_and_product.return_value = []
    h.products.delete_for_user.return_value = True

    outcome = await h.service.remove_alert(_TG, 1)

    assert outcome is AlertRemoval.removed_with_product
    h.products.delete_for_user.assert_awaited_once_with(10, _USER_ID)


def test_alert_removal_values_are_the_three_outcomes() -> None:
    assert {m.value for m in AlertRemoval} == {
        "not_found",
        "removed",
        "removed_with_product",
    }
