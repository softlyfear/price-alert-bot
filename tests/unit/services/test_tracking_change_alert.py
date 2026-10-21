"""Unit tests for ``TrackingService.change_alert_target`` (PAB-095).

The boundary mocked is the repository layer plus the session's
``begin_nested`` (as in ``test_tracking_service``). The real SQL, the
ownership predicate and the SAVEPOINT are proven on PostgreSQL in
``tests/integration/services/test_tracking_change_alert_live.py``. Here: the
decision tree, the scoping arguments, "no write unless something changed" and
the lost-race branches.
"""

from __future__ import annotations

from types import TracebackType
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.tracking as tracking_module
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.services.tracking import AlertChange
from app.services.tracking import TrackingService

_TG = 555
_USER_ID = 77  # deliberately different from the Telegram id
_PRODUCT_ID = 10
_ALERT_ID = 1
_PRICE = 100_000  # current price of the product, 1 000 rubles


def _alert(
    alert_id: int = _ALERT_ID,
    target: int = 90_000,
    direction: AlertDirection = AlertDirection.below,
) -> Alert:
    return Alert(
        id=alert_id,
        user_id=_USER_ID,
        product_id=_PRODUCT_ID,
        target_price=target,
        direction=direction,
    )


def _product() -> Product:
    return Product(
        id=_PRODUCT_ID,
        user_id=_USER_ID,
        marketplace=Marketplace.wb,
        article=12345678,
        product_name="P",
        current_price=_PRICE,
    )


class _Savepoint:
    """Async context manager standing in for ``AsyncSession.begin_nested()``."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> _Savepoint:
        self._events.append("savepoint_begin")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self._events.append(
            "savepoint_rollback" if exc_type is not None else "savepoint_release"
        )
        return False


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        session = MagicMock(spec=AsyncSession)
        session.begin_nested = MagicMock(side_effect=lambda: _Savepoint(self.events))
        self.users = MagicMock(spec=UserRepository)
        self.users.get_or_create_by_tg_id.return_value = (
            User(id=_USER_ID, tg_user_id=_TG),
            False,
        )
        self.products = MagicMock(spec=ProductRepository)
        self.products.get_by_id_for_user.return_value = _product()
        self.alerts = MagicMock(spec=AlertRepository)
        self.alerts.get_by_id_for_user.return_value = _alert()
        self.alerts.get_by_user_and_product.return_value = [_alert()]
        self.alerts.update_target_for_user.return_value = True
        monkeypatch.setattr(tracking_module, "UserRepository", lambda _s: self.users)
        monkeypatch.setattr(
            tracking_module, "ProductRepository", lambda _s: self.products
        )
        monkeypatch.setattr(tracking_module, "AlertRepository", lambda _s: self.alerts)
        self.service = TrackingService(
            session=cast(AsyncSession, session),
            http_client=httpx.AsyncClient(),
            client_factory=MagicMock(),
            max_products_per_user=50,
        )

    async def change(self, target: int) -> AlertChange:
        return await self.service.change_alert_target(_TG, _ALERT_ID, target)


@pytest.fixture
def h(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(monkeypatch)


# --- not found -------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_of_someone_else_or_missing_is_not_found_and_nothing_is_written(
    h: _Harness,
) -> None:
    h.alerts.get_by_id_for_user.return_value = None

    assert await h.change(80_000) is AlertChange.not_found

    h.alerts.get_by_id_for_user.assert_awaited_once_with(_ALERT_ID, _USER_ID)
    h.products.get_by_id_for_user.assert_not_called()
    h.alerts.update_target_for_user.assert_not_called()
    assert h.events == []


@pytest.mark.asyncio
async def test_alert_without_an_owned_product_is_not_found_and_nothing_is_written(
    h: _Harness,
) -> None:
    h.products.get_by_id_for_user.return_value = None

    assert await h.change(80_000) is AlertChange.not_found

    h.products.get_by_id_for_user.assert_awaited_once_with(_PRODUCT_ID, _USER_ID)
    h.alerts.update_target_for_user.assert_not_called()
    assert h.events == []


@pytest.mark.asyncio
async def test_update_that_matches_no_row_is_not_found(h: _Harness) -> None:
    """The alert was removed between the read and the write."""
    h.alerts.update_target_for_user.return_value = False

    assert await h.change(80_000) is AlertChange.not_found


# --- direction -------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_equal_to_the_current_price_is_refused_before_any_write(
    h: _Harness,
) -> None:
    with pytest.raises(TargetEqualsCurrentPriceError):
        await h.change(_PRICE)

    h.alerts.update_target_for_user.assert_not_called()
    assert h.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "direction"),
    [(_PRICE + 1, AlertDirection.above), (_PRICE - 1, AlertDirection.below)],
)
async def test_one_kopeck_either_side_of_the_price_picks_each_direction(
    h: _Harness, target: int, direction: AlertDirection
) -> None:
    assert await h.change(target) is AlertChange.changed

    h.alerts.update_target_for_user.assert_awaited_once_with(
        _ALERT_ID, _USER_ID, target, direction
    )


@pytest.mark.asyncio
async def test_changed_scopes_every_call_to_the_internal_user_not_the_telegram_id(
    h: _Harness,
) -> None:
    await h.change(80_000)

    h.users.get_or_create_by_tg_id.assert_awaited_once_with(_TG)
    h.alerts.update_target_for_user.assert_awaited_once_with(
        _ALERT_ID, _USER_ID, 80_000, AlertDirection.below
    )
    assert h.events == ["savepoint_begin", "savepoint_release"]


# --- unchanged -------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_amount_and_direction_is_unchanged_and_writes_nothing(
    h: _Harness,
) -> None:
    assert await h.change(90_000) is AlertChange.unchanged

    h.alerts.update_target_for_user.assert_not_called()
    assert h.events == []  # no savepoint either


@pytest.mark.asyncio
async def test_same_amount_with_a_flipped_direction_is_a_change(h: _Harness) -> None:
    """The price crossed the threshold since it was created: the side flipped."""
    h.alerts.get_by_id_for_user.return_value = _alert(
        target=90_000, direction=AlertDirection.above
    )

    assert await h.change(90_000) is AlertChange.changed

    h.alerts.update_target_for_user.assert_awaited_once_with(
        _ALERT_ID, _USER_ID, 90_000, AlertDirection.below
    )


@pytest.mark.asyncio
async def test_same_direction_with_another_amount_is_a_change(h: _Harness) -> None:
    assert await h.change(90_001) is AlertChange.changed

    h.alerts.update_target_for_user.assert_awaited_once_with(
        _ALERT_ID, _USER_ID, 90_001, AlertDirection.below
    )


# --- duplicates ------------------------------------------------------------


@pytest.mark.asyncio
async def test_amount_and_direction_of_another_alert_is_a_duplicate_and_not_written(
    h: _Harness,
) -> None:
    h.alerts.get_by_user_and_product.return_value = [
        _alert(),
        _alert(alert_id=2, target=80_000),
    ]

    with pytest.raises(DuplicateAlertError):
        await h.change(80_000)

    h.alerts.update_target_for_user.assert_not_called()
    assert h.events == []


@pytest.mark.asyncio
async def test_same_amount_on_the_other_side_of_the_price_is_not_a_duplicate(
    h: _Harness,
) -> None:
    h.alerts.get_by_user_and_product.return_value = [
        _alert(),
        _alert(alert_id=2, target=120_000, direction=AlertDirection.above),
    ]

    assert await h.change(80_000) is AlertChange.changed


@pytest.mark.asyncio
async def test_the_alert_itself_is_never_its_own_duplicate(h: _Harness) -> None:
    """A concurrent edit already stored this very value on the same row."""
    h.alerts.get_by_user_and_product.return_value = [_alert(target=80_000)]

    assert await h.change(80_000) is AlertChange.changed

    h.alerts.update_target_for_user.assert_awaited_once()


# --- lost race on the unique key -------------------------------------------


@pytest.mark.asyncio
async def test_key_collision_with_a_found_duplicate_becomes_duplicate_error(
    h: _Harness,
) -> None:
    collision = IntegrityError("UPDATE", {}, Exception("uq_alerts_user_product_logic"))
    h.alerts.update_target_for_user.side_effect = collision
    h.alerts.get_by_user_and_product.side_effect = [
        [_alert()],
        [_alert(), _alert(alert_id=2, target=80_000)],
    ]

    with pytest.raises(DuplicateAlertError) as excinfo:
        await h.change(80_000)

    assert excinfo.value.__cause__ is collision
    assert h.events == ["savepoint_begin", "savepoint_rollback"]


@pytest.mark.asyncio
async def test_key_collision_without_a_duplicate_propagates_the_integrity_error(
    h: _Harness,
) -> None:
    collision = IntegrityError("UPDATE", {}, Exception("check violated"))
    h.alerts.update_target_for_user.side_effect = collision

    with pytest.raises(IntegrityError) as excinfo:
        await h.change(80_000)

    assert excinfo.value is collision
    assert h.events == ["savepoint_begin", "savepoint_rollback"]
