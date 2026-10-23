"""Unit tests for ``PriceService.batch_size`` / ``fetch_prices`` / ``apply_result``.

PAB-081 AC3. Collaborators are mocks (the harness of ``test_price_service.py``);
no database, no network. Time is frozen through the ``datetime`` name of the
module under test, so ``last_checked_at`` is compared for equality, not against
a real-clock bracket.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.price_service as price_service_module
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.notification import NotificationService
from app.services.price_service import PriceService
from tests.unit.services.test_price_service import _build_harness
from tests.unit.services.test_price_service import _FrozenClock
from tests.unit.services.test_price_service import _Harness
from tests.unit.services.test_price_service import _make_alert
from tests.unit.services.test_price_service import _make_product
from tests.unit.services.test_price_service import _make_user
from tests.unit.wb_access_stub import stub_client_factory

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
_TARGET = 100_000


def _http() -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, MagicMock(spec=httpx.AsyncClient))


def _session(harness: _Harness) -> AsyncSession:
    return cast(AsyncSession, harness.session)


def _flush(harness: _Harness) -> AsyncMock:
    return cast(AsyncMock, harness.session.flush)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(_NOW))
    return _NOW


# --- apply_result: thresholds, both directions, one kopeck either side --------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "price", "fires"),
    [
        (AlertDirection.below, _TARGET - 1, True),
        (AlertDirection.below, _TARGET, True),
        (AlertDirection.below, _TARGET + 1, False),
        (AlertDirection.above, _TARGET + 1, True),
        (AlertDirection.above, _TARGET, True),
        (AlertDirection.above, _TARGET - 1, False),
    ],
)
async def test_apply_result_fires_exactly_on_the_threshold_side(
    direction: AlertDirection, price: int, fires: bool
) -> None:
    """AC3(а). Mutations: ``<=`` -> ``<`` / ``>=`` -> ``>``; direction swapped."""
    product = _make_product(current_price=123_456)
    alert = _make_alert(direction=direction, target_price=_TARGET)
    harness = _build_harness(
        product=product,
        market_data=MarketplaceProductData(name="Товар", price=price),
        alerts=[alert],
        user=_make_user(),
    )

    await harness.service.apply_result(
        1, MarketplaceProductData(name="Товар", price=price), _session(harness)
    )

    assert harness.notification.send_alert.call_count == (1 if fires else 0)
    assert product.current_price == price
    assert product.previous_price == 123_456
    _flush(harness).assert_called_once()


@pytest.mark.asyncio
async def test_apply_result_sends_the_alert_with_its_product_and_user() -> None:
    """AC3(а): the notification receives this alert, product and user."""
    product = _make_product(current_price=200_000)
    alert = _make_alert(target_price=_TARGET)
    user = _make_user()
    result = MarketplaceProductData(name="Товар", price=90_000)
    harness = _build_harness(
        product=product, market_data=result, alerts=[alert], user=user
    )

    await harness.service.apply_result(1, result, _session(harness))

    harness.notification.send_alert.assert_called_once_with(
        alert=alert, product=product, user=user
    )


@pytest.mark.asyncio
async def test_apply_result_writes_last_checked_at_from_the_clock(
    frozen_now: datetime,
) -> None:
    """AC3(а): the check time is the (frozen) UTC now."""
    product = _make_product(current_price=200_000)
    result = MarketplaceProductData(name="Товар", price=190_000)
    harness = _build_harness(product=product, market_data=result)

    await harness.service.apply_result(1, result, _session(harness))

    assert product.last_checked_at == frozen_now


# --- apply_result: cooldown, delivery failure ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "fires"),
    [
        (timedelta(seconds=3600), True),
        (timedelta(seconds=3600) - timedelta(microseconds=1), False),
        (timedelta(minutes=5), False),
        (timedelta(hours=2), True),
    ],
    ids=["exactly_cooldown", "one_microsecond_short", "inside", "past"],
)
async def test_apply_result_respects_the_cooldown_counted_from_triggered_at(
    frozen_now: datetime, age: timedelta, fires: bool
) -> None:
    """AC3(а). Mutations: ``<`` -> ``<=`` on the cooldown; cooldown ignored."""
    product = _make_product(current_price=200_000)
    alert = _make_alert(target_price=_TARGET, triggered_at=frozen_now - age)
    result = MarketplaceProductData(name="Товар", price=90_000)
    harness = _build_harness(
        product=product, market_data=result, alerts=[alert], user=_make_user()
    )

    await harness.service.apply_result(1, result, _session(harness))

    assert harness.notification.send_alert.call_count == (1 if fires else 0)


@pytest.mark.asyncio
async def test_apply_result_survives_a_failed_delivery_and_keeps_the_price() -> None:
    """AC3(а). Mutation: drop the ``except`` (the exception reaches the caller
    and the price write is rolled back by the scheduler)."""
    product = _make_product(current_price=200_000)
    result = MarketplaceProductData(name="Товар", price=90_000)
    harness = _build_harness(
        product=product,
        market_data=result,
        alerts=[_make_alert(alert_id=7, target_price=_TARGET)],
        user=_make_user(),
    )
    harness.notification.send_alert.side_effect = RuntimeError("telegram down")
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    try:
        await harness.service.apply_result(1, result, _session(harness))
    finally:
        logger.remove(sink_id)

    assert product.current_price == 90_000
    _flush(harness).assert_called_once()
    errors = [r for r in records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["extra"]["alert_id"] == 7
    assert errors[0]["extra"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_apply_result_never_assigns_triggered_at() -> None:
    """Only ``NotificationService.send_alert`` writes ``triggered_at``."""
    product = _make_product(current_price=200_000)
    alert = _make_alert(target_price=_TARGET)
    result = MarketplaceProductData(name="Товар", price=90_000)
    harness = _build_harness(
        product=product, market_data=result, alerts=[alert], user=_make_user()
    )

    await harness.service.apply_result(1, result, _session(harness))

    assert alert.triggered_at is None


# --- apply_result: failures ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "level"),
    [
        (FetchFailureReason.not_found, "WARNING"),
        (FetchFailureReason.blocked, "WARNING"),
        (FetchFailureReason.transport_error, "WARNING"),
        (FetchFailureReason.bad_payload, "ERROR"),
        (FetchFailureReason.out_of_stock, "INFO"),
    ],
)
async def test_apply_result_failure_changes_only_last_checked_at(
    frozen_now: datetime, reason: FetchFailureReason, level: str
) -> None:
    """AC3(б). Each category: price untouched, no alert evaluation, one flush, one
    record at the level of PROJECT.md 8.4 carrying the product context."""
    product = _make_product(current_price=142_900, previous_price=100_000)
    failure = MarketplaceFetchFailure(reason=reason, detail="why")
    harness = _build_harness(
        product=product,
        market_data=failure,
        alerts=[_make_alert(target_price=_TARGET)],
        user=_make_user(),
    )
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    try:
        await harness.service.apply_result(1, failure, _session(harness))
    finally:
        logger.remove(sink_id)

    assert product.current_price == 142_900
    assert product.previous_price == 100_000
    assert product.last_checked_at == frozen_now
    harness.alert_repo.get_active_by_product.assert_not_called()
    harness.user_repo.get_by_id.assert_not_called()
    harness.notification.send_alert.assert_not_called()
    _flush(harness).assert_called_once()
    assert len(records) == 1
    assert records[0]["level"].name == level
    assert records[0]["extra"]["reason"] == reason.value
    assert records[0]["extra"]["detail"] == "why"
    assert records[0]["extra"]["product_id"] == product.id


@pytest.mark.asyncio
async def test_apply_result_for_a_deleted_product_is_a_silent_no_op() -> None:
    """AC3(в). Mutation: ``product is None`` check dropped (``AttributeError``)."""
    harness = _build_harness(
        product=None, market_data=MarketplaceProductData(name="Товар", price=100)
    )

    await harness.service.apply_result(
        1, MarketplaceProductData(name="Товар", price=100), _session(harness)
    )

    harness.product_repo.get_by_id.assert_called_once_with(1)
    harness.alert_repo.get_active_by_product.assert_not_called()
    harness.notification.send_alert.assert_not_called()
    _flush(harness).assert_not_called()


@pytest.mark.asyncio
async def test_apply_result_makes_no_marketplace_call() -> None:
    """AC3: applying a result is storage only; the network stays untouched."""
    product = _make_product(current_price=200_000)
    result = MarketplaceProductData(name="Товар", price=190_000)
    harness = _build_harness(product=product, market_data=result)

    await harness.service.apply_result(1, result, _session(harness))

    harness.client.get_product_data.assert_not_called()
    harness.client.get_products_data.assert_not_called()


@pytest.mark.asyncio
async def test_apply_result_with_a_naive_triggered_at_raises_type_error() -> None:
    """The invariant of ``check_product`` holds here too: a violated one raises
    (the scheduler isolates it per product)."""
    product = _make_product(current_price=200_000)
    alert = _make_alert(target_price=_TARGET, triggered_at=datetime(2020, 1, 1))  # noqa: DTZ001
    result = MarketplaceProductData(name="Товар", price=90_000)
    harness = _build_harness(
        product=product, market_data=result, alerts=[alert], user=_make_user()
    )

    with pytest.raises(TypeError):
        await harness.service.apply_result(1, result, _session(harness))


# --- check_product == fetch + apply --------------------------------------------


_SCENARIOS: dict[str, MarketplaceFetchResult] = {
    "fires": MarketplaceProductData(name="Товар", price=90_000),
    "quiet": MarketplaceProductData(name="Товар", price=150_000),
    "failure": MarketplaceFetchFailure(reason=FetchFailureReason.blocked, detail="x"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("result", list(_SCENARIOS.values()), ids=list(_SCENARIOS))
async def test_check_product_and_apply_result_leave_the_same_state(
    frozen_now: datetime, result: MarketplaceFetchResult
) -> None:
    """AC3(а), AC3(д): the thin wrapper and the new entry point write identically."""

    def build() -> tuple[_Harness, object]:
        product = _make_product(current_price=200_000, previous_price=1)
        harness = _build_harness(
            product=product,
            market_data=result,
            alerts=[_make_alert(target_price=_TARGET)],
            user=_make_user(),
        )
        return harness, product

    via_check, product_a = build()
    via_apply, product_b = build()

    await via_check.service.check_product(1, _session(via_check))
    await via_apply.service.apply_result(1, result, _session(via_apply))

    assert vars(product_a).keys() == vars(product_b).keys()
    for key in vars(product_a):
        if key != "_sa_instance_state":
            assert vars(product_a)[key] == vars(product_b)[key], key
    assert (
        via_check.notification.send_alert.call_count
        == via_apply.notification.send_alert.call_count
    )
    assert _flush(via_check).call_count == _flush(via_apply).call_count


# --- batch_size / fetch_prices ---------------------------------------------------


def _service_with_factory(
    factory_calls: list[tuple[Marketplace, httpx.AsyncClient]],
    client: BaseMarketplaceClient,
    http: httpx.AsyncClient,
) -> PriceService:
    def factory(
        marketplace: Marketplace, http_client: httpx.AsyncClient
    ) -> BaseMarketplaceClient:
        factory_calls.append((marketplace, http_client))
        return client

    return PriceService(
        notification_service=cast(NotificationService, MagicMock()),
        product_repo_factory=lambda _s: cast(ProductRepository, MagicMock()),
        alert_repo_factory=lambda _s: cast(AlertRepository, MagicMock()),
        user_repo_factory=lambda _s: cast(UserRepository, MagicMock()),
        client_factory=factory,
        http_client=http,
        alert_cooldown_seconds=3600,
    )


@pytest.mark.asyncio
async def test_fetch_prices_returns_the_batch_of_the_marketplace_client() -> None:
    """AC3(г). Mutations: another marketplace/client passed; articles changed;
    the result post-processed."""
    http = _http()
    expected: dict[int, MarketplaceFetchResult] = {
        5: MarketplaceProductData(name="Пять", price=500),
        6: MarketplaceFetchFailure(reason=FetchFailureReason.not_found),
    }
    client = MagicMock(spec=BaseMarketplaceClient)
    client.get_products_data.return_value = expected
    calls: list[tuple[Marketplace, httpx.AsyncClient]] = []
    service = _service_with_factory(calls, cast(BaseMarketplaceClient, client), http)

    result = await service.fetch_prices(Marketplace.ozon, [5, 6])

    assert result == expected
    assert calls == [(Marketplace.ozon, http)]
    client.get_products_data.assert_called_once_with([5, 6])
    client.get_product_data.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_prices_lets_unsupported_marketplace_error_through() -> None:
    """AC3(г): a configuration error is not turned into a marked failure."""
    http = _http()
    service = PriceService(
        notification_service=cast(NotificationService, MagicMock()),
        product_repo_factory=lambda _s: cast(ProductRepository, MagicMock()),
        alert_repo_factory=lambda _s: cast(AlertRepository, MagicMock()),
        user_repo_factory=lambda _s: cast(UserRepository, MagicMock()),
        client_factory=stub_client_factory(),
        http_client=http,
        alert_cooldown_seconds=3600,
    )
    bogus = cast(Marketplace, "amazon")

    with pytest.raises(UnsupportedMarketplaceError):
        await service.fetch_prices(bogus, [1])
    with pytest.raises(UnsupportedMarketplaceError):
        service.batch_size(bogus)


@pytest.mark.asyncio
async def test_batch_size_is_one_for_ozon_and_a_hundred_for_wb() -> None:
    """AC3(г): the real factory, literals from the ticket."""
    http = _http()
    service = PriceService(
        notification_service=cast(NotificationService, MagicMock()),
        product_repo_factory=lambda _s: cast(ProductRepository, MagicMock()),
        alert_repo_factory=lambda _s: cast(AlertRepository, MagicMock()),
        user_repo_factory=lambda _s: cast(UserRepository, MagicMock()),
        client_factory=stub_client_factory(),
        http_client=http,
        alert_cooldown_seconds=3600,
    )
    assert service.batch_size(Marketplace.ozon) == 1
    assert service.batch_size(Marketplace.wb) == 100


def test_batch_size_asks_the_factory_with_the_marketplace_and_shared_http_client() -> (
    None
):
    """AC3(г). Mutation: a hard-coded size instead of the client's attribute."""
    http = _http()
    client = MagicMock(spec=BaseMarketplaceClient)
    client.batch_size = 37
    calls: list[tuple[Marketplace, httpx.AsyncClient]] = []
    service = _service_with_factory(calls, cast(BaseMarketplaceClient, client), http)

    assert service.batch_size(Marketplace.wb) == 37
    assert calls == [(Marketplace.wb, http)]


@pytest.mark.asyncio
@pytest.mark.parametrize("card_price", [95_000, None])
async def test_apply_result_stores_the_fresh_card_price(card_price: int | None) -> None:
    product = _make_product(current_price=123_456)
    product.card_price = 1
    data = MarketplaceProductData(name="Товар", price=100_000, card_price=card_price)
    harness = _build_harness(
        product=product, market_data=data, alerts=[], user=_make_user()
    )

    await harness.service.apply_result(1, data, _session(harness))

    assert product.card_price == card_price
