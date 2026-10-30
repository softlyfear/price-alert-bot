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
        self.users.get_by_tg_id.return_value = (
            self.users.get_or_create_by_tg_id.return_value[0]
        )
        self.products = MagicMock(spec=ProductRepository)
        self.products.count_before.return_value = 0
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


# --- list_page / list_page_of (PAB-108) -----------------------------------


@pytest.mark.asyncio
async def test_list_page_reads_the_requested_window_for_the_internal_user_id(
    h: _Harness,
) -> None:
    h.products.count_by_user.return_value = 23
    h.products.get_page_by_user.return_value = [_product(21), _product(22)]

    page = await h.service.list_page(_TG, 3)

    assert [p.id for p in page.products] == [21, 22]
    assert (page.total, page.page, page.pages) == (23, 3, 3)
    h.users.get_by_tg_id.assert_awaited_once_with(_TG)
    h.products.count_by_user.assert_awaited_once_with(_USER_ID)
    h.products.get_page_by_user.assert_awaited_once_with(_USER_ID, 10, 20)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asked", "shown", "offset"), [(0, 1, 0), (-5, 1, 0), (99, 3, 20), (2, 2, 10)]
)
async def test_list_page_clamps_the_page_into_one_to_pages(
    h: _Harness, asked: int, shown: int, offset: int
) -> None:
    h.products.count_by_user.return_value = 23
    h.products.get_page_by_user.return_value = []

    page = await h.service.list_page(_TG, asked)

    assert page.page == shown
    h.products.get_page_by_user.assert_awaited_once_with(_USER_ID, 10, offset)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total", "pages"), [(0, 1), (1, 1), (10, 1), (11, 2), (20, 2), (21, 3)]
)
async def test_list_page_counts_pages_as_ceil_of_total_by_ten_and_at_least_one(
    h: _Harness, total: int, pages: int
) -> None:
    h.products.count_by_user.return_value = total
    h.products.get_page_by_user.return_value = []

    page = await h.service.list_page(_TG, 1)

    assert (page.total, page.pages) == (total, pages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "total", "expected"),
    [(0, 23, 1), (9, 23, 1), (10, 23, 2), (14, 23, 2), (22, 23, 3), (30, 23, 3)],
)
async def test_list_page_of_is_before_over_ten_plus_one_clamped_to_pages(
    h: _Harness, before: int, total: int, expected: int
) -> None:
    h.products.count_by_user.return_value = total
    h.products.count_before.return_value = before

    assert await h.service.list_page_of(_TG, 500) == expected

    h.products.count_before.assert_awaited_once_with(_USER_ID, 500)
    h.products.count_by_user.assert_awaited_once_with(_USER_ID)


@pytest.mark.asyncio
async def test_list_page_of_with_no_products_left_is_page_one(h: _Harness) -> None:
    h.products.count_by_user.return_value = 0
    h.products.count_before.return_value = 0

    assert await h.service.list_page_of(_TG, 7) == 1


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
    assert card.list_page == 1
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "page"), [(0, 1), (9, 1), (10, 2), (22, 3)])
async def test_get_product_card_list_page_is_the_page_holding_the_product(
    h: _Harness, before: int, page: int
) -> None:
    h.products.get_by_id_for_user.return_value = _product(10)
    h.alerts.get_by_user_and_product.return_value = []
    h.products.count_before.return_value = before

    card = await h.service.get_product_card(_TG, 10)

    assert card is not None
    assert card.list_page == page
    h.products.count_before.assert_awaited_once_with(_USER_ID, 10)
