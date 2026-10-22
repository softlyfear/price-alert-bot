"""PAB-099 AC3: ``PriceService`` writes ``products.price_fetched_at``, live.

Real repositories on PostgreSQL; the notification service and the marketplace
client are the replaced boundaries. The clock of ``app.services.price_service``
is frozen to a literal, so the expected values are written out by hand and not
read back from the code under test. Both entry points (``apply_result`` and
``check_product``) run through the same ``_apply`` and are checked alike.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.price_service as price_service_module
from app.models.product import Product
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient
from app.services.notification import NotificationService
from app.services.price_service import PriceService
from tests.integration.repositories.conftest import make_product
from tests.integration.repositories.conftest import make_user

_NOW = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
_OLD_STAMP = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
_OLD_CHECK = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


class _FrozenDatetime:
    """Stands in for the ``datetime`` name of the module under test."""

    @staticmethod
    def now(tz: object = None) -> datetime:
        return _NOW


class _Client:
    """Marketplace boundary: answers every request with the same result."""

    def __init__(self, result: MarketplaceFetchResult) -> None:
        self._result = result

    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        return self._result


_Entry = Callable[
    [PriceService, int, AsyncSession, MarketplaceFetchResult], Awaitable[None]
]


async def _via_apply_result(
    service: PriceService,
    product_id: int,
    session: AsyncSession,
    result: MarketplaceFetchResult,
) -> None:
    await service.apply_result(product_id, result, session)


async def _via_check_product(
    service: PriceService,
    product_id: int,
    session: AsyncSession,
    result: MarketplaceFetchResult,
) -> None:
    await service.check_product(product_id, session)


_ENTRIES = [
    pytest.param(_via_apply_result, id="apply_result"),
    pytest.param(_via_check_product, id="check_product"),
]


def _service(result: MarketplaceFetchResult) -> PriceService:
    client = _Client(result)
    return PriceService(
        notification_service=MagicMock(spec=NotificationService),
        product_repo_factory=ProductRepository,
        alert_repo_factory=AlertRepository,
        user_repo_factory=UserRepository,
        client_factory=lambda _marketplace, _http: cast(BaseMarketplaceClient, client),
        http_client=httpx.AsyncClient(),
        alert_cooldown_seconds=3600,
    )


async def _seed(session: AsyncSession, stamp: datetime | None) -> int:
    user = await make_user(session)
    product = await make_product(session, user_id=user.id, current_price=10_000)
    product.price_fetched_at = stamp
    product.last_checked_at = _OLD_CHECK
    await session.flush()
    return product.id


async def _stored(
    session: AsyncSession, product_id: int
) -> tuple[datetime | None, datetime | None, int]:
    row = (
        await session.execute(
            select(
                Product.price_fetched_at, Product.last_checked_at, Product.current_price
            ).where(Product.id == product_id)
        )
    ).one()
    return row[0], row[1], row[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("old", [None, _OLD_STAMP], ids=["null", "set"])
async def test_success_sets_price_fetched_at_to_the_check_time(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    entry: _Entry,
    old: datetime | None,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenDatetime)
    product_id = await _seed(db_session, old)
    result = MarketplaceProductData(name="Item", price=9_500)

    await entry(_service(result), product_id, db_session, result)

    fetched, checked, price = await _stored(db_session, product_id)
    assert fetched == _NOW
    assert fetched is not None and fetched.utcoffset() is not None
    assert checked == _NOW
    assert price == 9_500


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("reason", list(FetchFailureReason))
@pytest.mark.parametrize("old", [None, _OLD_STAMP], ids=["null", "set"])
async def test_failure_keeps_price_fetched_at_and_moves_last_checked_at(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    entry: _Entry,
    reason: FetchFailureReason,
    old: datetime | None,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenDatetime)
    product_id = await _seed(db_session, old)
    result = MarketplaceFetchFailure(reason=reason, detail="stub")

    await entry(_service(result), product_id, db_session, result)

    fetched, checked, price = await _stored(db_session, product_id)
    assert fetched == old
    assert checked == _NOW  # moved from _OLD_CHECK: the attempt is recorded
    assert price == 10_000


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", _ENTRIES)
async def test_success_changes_current_price_but_not_initial_price(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, entry: _Entry
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenDatetime)
    product_id = await _seed(db_session, None)
    await _set_initial(db_session, product_id, 10_000)
    result = MarketplaceProductData(name="Item", price=9_500)

    await entry(_service(result), product_id, db_session, result)

    assert (await _stored(db_session, product_id))[2] == 9_500  # control
    assert await _initial(db_session, product_id) == 10_000


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("reason", list(FetchFailureReason))
async def test_failure_keeps_initial_price(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    entry: _Entry,
    reason: FetchFailureReason,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenDatetime)
    product_id = await _seed(db_session, None)
    await _set_initial(db_session, product_id, 10_000)
    result = MarketplaceFetchFailure(reason=reason, detail="stub")

    await entry(_service(result), product_id, db_session, result)

    assert (await _stored(db_session, product_id))[0] is None  # not fetched
    assert await _initial(db_session, product_id) == 10_000


async def _set_initial(session: AsyncSession, product_id: int, value: int) -> None:
    product = (
        await session.execute(select(Product).where(Product.id == product_id))
    ).scalar_one()
    product.initial_price = value
    await session.flush()


async def _initial(session: AsyncSession, product_id: int) -> int | None:
    return (
        await session.execute(
            select(Product.initial_price).where(Product.id == product_id)
        )
    ).scalar_one()
