"""Unit tests for app.services.price_service.PriceService.

Every collaborator - the three repositories, the marketplace client and
`NotificationService` - is a mock; no test in this module touches a
database or the network, per the requirement that the domain layer be
testable on mocks alone.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import NamedTuple
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.price_service as price_service_module

if TYPE_CHECKING:
    # `Message`/`Record` only exist in loguru's bundled `.pyi` stub, not at
    # runtime in `loguru/__init__.py`.
    from loguru import Message
    from loguru import Record

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
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

_ARTICLE = 860043555
_COOLDOWN_SECONDS = 3600


def _make_product(
    *,
    current_price: int = 142900,
    previous_price: int | None = None,
    last_checked_at: datetime | None = None,
) -> Product:
    return Product(
        id=1,
        user_id=1,
        marketplace=Marketplace.wb,
        article=_ARTICLE,
        product_name="Товар",
        current_price=current_price,
        previous_price=previous_price,
        last_checked_at=last_checked_at,
    )


def _make_alert(
    *,
    alert_id: int = 1,
    direction: AlertDirection = AlertDirection.below,
    target_price: int = 142900,
    triggered_at: datetime | None = None,
) -> Alert:
    return Alert(
        id=alert_id,
        user_id=1,
        product_id=1,
        target_price=target_price,
        direction=direction,
        is_active=True,
        triggered_at=triggered_at,
    )


def _make_user() -> User:
    return User(id=1, tg_user_id=100500, tg_username="user")


class _Harness(NamedTuple):
    """A `PriceService` plus the raw mocks a test asserts against."""

    service: PriceService
    session: MagicMock
    product_repo: MagicMock
    alert_repo: MagicMock
    user_repo: MagicMock
    client: MagicMock
    notification: MagicMock


def _build_harness(
    *,
    product: Product | None,
    market_data: MarketplaceFetchResult,
    alerts: list[Alert] | None = None,
    user: User | None = None,
    cooldown_seconds: int = _COOLDOWN_SECONDS,
) -> _Harness:
    """Wire a `PriceService` with mocked collaborators.

    `MagicMock(spec=...)` auto-detects `async def` methods on the spec class
    and replaces them with `AsyncMock` (stdlib behaviour since Python 3.8),
    so every awaited collaborator method below is already awaitable without
    an explicit `AsyncMock(...)` wrapper.
    """
    product_repo = MagicMock(spec=ProductRepository)
    product_repo.get_by_id.return_value = product

    alert_repo = MagicMock(spec=AlertRepository)
    alert_repo.get_active_by_product.return_value = alerts or []

    user_repo = MagicMock(spec=UserRepository)
    user_repo.get_by_id.return_value = user

    client = MagicMock(spec=BaseMarketplaceClient)
    client.get_product_data.return_value = market_data

    notification = MagicMock(spec=NotificationService)
    notification.send_alert.return_value = None

    session = MagicMock(spec=AsyncSession)

    service = PriceService(
        notification_service=cast(NotificationService, notification),
        product_repo_factory=lambda _session: cast(ProductRepository, product_repo),
        alert_repo_factory=lambda _session: cast(AlertRepository, alert_repo),
        user_repo_factory=lambda _session: cast(UserRepository, user_repo),
        client_factory=lambda _marketplace, _http: cast(BaseMarketplaceClient, client),
        http_client=cast(httpx.AsyncClient, MagicMock(spec=httpx.AsyncClient)),
        alert_cooldown_seconds=cooldown_seconds,
    )
    return _Harness(
        service=service,
        session=session,
        product_repo=product_repo,
        alert_repo=alert_repo,
        user_repo=user_repo,
        client=client,
        notification=notification,
    )


class _FrozenClock:
    """Stand-in for the `datetime` name in `price_service_module`.

    Used only by the two exact-boundary cooldown tests below: real wall time
    cannot reliably hit an exact instant, and the ticket's boundary AC
    (equality, not merely "far apart") needs one. Every other cooldown cell
    in this module uses a plain relative delta against real time instead,
    per the ticket hint that this is cheaper and needs no clock control.
    """

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self, tz: object = None) -> datetime:
        return self._instant


@pytest.mark.asyncio
async def test_check_product_returns_early_when_product_is_missing() -> None:
    harness = _build_harness(
        product=None, market_data=MarketplaceProductData(name="Товар", price=100)
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.product_repo.get_by_id.assert_called_once_with(1)
    harness.client.get_product_data.assert_not_called()
    harness.alert_repo.get_active_by_product.assert_not_called()
    harness.user_repo.get_by_id.assert_not_called()
    cast(AsyncMock, harness.session.flush).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_level"),
    [
        (FetchFailureReason.not_found, "WARNING"),
        (FetchFailureReason.blocked, "WARNING"),
        (FetchFailureReason.transport_error, "WARNING"),
        (FetchFailureReason.bad_payload, "ERROR"),
        (FetchFailureReason.out_of_stock, "INFO"),
    ],
)
async def test_fetch_failure_updates_timestamp_only_and_logs_expected_level(
    reason: FetchFailureReason, expected_level: str
) -> None:
    """AC3 (+ поправка 2026-09-23 для пятой категории): каждая из пяти
    категорий обновляет только `last_checked_at`, не трогает цену и пишет в
    лог на уровне из §8.4 с полем `reason`."""
    product = _make_product(current_price=142900, previous_price=100000)
    failure = MarketplaceFetchFailure(reason=reason, detail="boom")
    harness = _build_harness(product=product, market_data=failure)

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    before = datetime.now(UTC)
    try:
        await harness.service.check_product(1, cast(AsyncSession, harness.session))
    finally:
        logger.remove(sink_id)
    after = datetime.now(UTC)

    assert product.current_price == 142900
    assert product.previous_price == 100000
    assert product.last_checked_at is not None
    assert before <= product.last_checked_at <= after

    harness.alert_repo.get_active_by_product.assert_not_called()
    harness.user_repo.get_by_id.assert_not_called()
    cast(AsyncMock, harness.session.flush).assert_called_once()

    assert len(records) == 1
    assert records[0]["level"].name == expected_level
    assert records[0]["extra"]["reason"] == reason.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "direction",
        "previous_price",
        "current_price",
        "target_price",
        "expected_triggered",
    ),
    [
        # below: strictly under target - triggers under both edge and level logic.
        (AlertDirection.below, 150000, 100000, 120000, True),
        # below: exactly at threshold - the level predicate is `<=`, so it fires.
        (AlertDirection.below, 150000, 120000, 120000, True),
        # below: strictly above target - never fires.
        (AlertDirection.below, 150000, 130000, 120000, False),
        # AC4: price never moved (previous == current == target). The
        # retired edge predicate (`previous > target >= current`) is False
        # here (150000 > 150000 is False); the level predicate is True.
        (AlertDirection.below, 150000, 150000, 150000, True),
        # above (AC6, no UI path but must be exercised): strictly over - fires.
        (AlertDirection.above, 100000, 130000, 120000, True),
        # above: exactly at threshold - fires.
        (AlertDirection.above, 100000, 120000, 120000, True),
        # above: strictly under - never fires.
        (AlertDirection.above, 100000, 110000, 120000, False),
    ],
)
async def test_predicate_matrix(
    direction: AlertDirection,
    previous_price: int,
    current_price: int,
    target_price: int,
    expected_triggered: bool,
) -> None:
    product = _make_product(current_price=previous_price)
    alert = _make_alert(direction=direction, target_price=target_price)
    market_data = MarketplaceProductData(name="Товар", price=current_price)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=_make_user()
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    assert harness.notification.send_alert.called is expected_triggered
    assert product.previous_price == previous_price
    assert product.current_price == current_price


@pytest.mark.asyncio
async def test_cooldown_none_sends() -> None:
    product = _make_product(current_price=100000)
    alert = _make_alert(target_price=120000, triggered_at=None)
    market_data = MarketplaceProductData(name="Товар", price=100000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=_make_user()
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.notification.send_alert.assert_called_once()


@pytest.mark.asyncio
async def test_cooldown_within_window_suppresses() -> None:
    product = _make_product(current_price=100000)
    triggered_at = datetime.now(UTC) - timedelta(seconds=_COOLDOWN_SECONDS // 2)
    alert = _make_alert(target_price=120000, triggered_at=triggered_at)
    market_data = MarketplaceProductData(name="Товар", price=100000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=_make_user()
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.notification.send_alert.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_past_window_sends() -> None:
    product = _make_product(current_price=100000)
    triggered_at = datetime.now(UTC) - timedelta(seconds=_COOLDOWN_SECONDS * 2)
    alert = _make_alert(target_price=120000, triggered_at=triggered_at)
    market_data = MarketplaceProductData(name="Товар", price=100000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=_make_user()
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.notification.send_alert.assert_called_once()


@pytest.mark.asyncio
async def test_cooldown_boundary_exact_equality_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5's equality cell: `now - triggered_at == cooldown` sends - the gate
    is `>=`, not `>`."""
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(fixed_now))
    cooldown = timedelta(seconds=_COOLDOWN_SECONDS)

    product = _make_product(current_price=100000)
    alert = _make_alert(target_price=120000, triggered_at=fixed_now - cooldown)
    market_data = MarketplaceProductData(name="Товар", price=100000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=_make_user()
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.notification.send_alert.assert_called_once()


@pytest.mark.asyncio
async def test_cooldown_boundary_one_microsecond_short_suppresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of the equality cell: one microsecond short of cooldown
    still suppresses."""
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(fixed_now))
    cooldown = timedelta(seconds=_COOLDOWN_SECONDS)

    product = _make_product(current_price=100000)
    triggered_at = fixed_now - cooldown + timedelta(microseconds=1)
    alert = _make_alert(target_price=120000, triggered_at=triggered_at)
    market_data = MarketplaceProductData(name="Товар", price=100000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=_make_user()
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.notification.send_alert.assert_not_called()


@pytest.mark.asyncio
async def test_naive_triggered_at_raises_type_error() -> None:
    """A naive `triggered_at` violates the timezone-aware UTC invariant
    (contract of the `DateTime(timezone=True)` column). `check_product` does
    not defend against it: the comparison against an aware `now_utc` is left
    to raise `TypeError` loudly rather than silently coerce it - this is the
    named decision for AC5's last cell."""
    product = _make_product(current_price=100000)
    naive_triggered_at = datetime(2020, 1, 1)  # noqa: DTZ001 - deliberately naive
    alert = _make_alert(target_price=120000, triggered_at=naive_triggered_at)
    market_data = MarketplaceProductData(name="Товар", price=100000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=_make_user()
    )

    with pytest.raises(TypeError):
        await harness.service.check_product(1, cast(AsyncSession, harness.session))


@pytest.mark.asyncio
async def test_delivery_failure_of_one_alert_does_not_block_the_rest() -> None:
    """AC8: a `send_alert` failure on one alert does not stop the loop, and
    the failure is logged with `alert_id`/`product_id`/`user_id` context."""
    product = _make_product(current_price=100000)
    failing_alert = _make_alert(alert_id=1, target_price=120000)
    other_alert = _make_alert(alert_id=2, target_price=150000)
    market_data = MarketplaceProductData(name="Товар", price=100000)
    user = _make_user()
    harness = _build_harness(
        product=product,
        market_data=market_data,
        alerts=[failing_alert, other_alert],
        user=user,
    )
    harness.notification.send_alert.side_effect = [RuntimeError("boom"), None]

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    try:
        await harness.service.check_product(1, cast(AsyncSession, harness.session))
    finally:
        logger.remove(sink_id)

    assert harness.notification.send_alert.call_count == 2
    error_records = [r for r in records if r["level"].name == "ERROR"]
    assert len(error_records) == 1
    assert error_records[0]["extra"]["alert_id"] == 1
    assert error_records[0]["extra"]["product_id"] == product.id
    assert error_records[0]["extra"]["user_id"] == user.id
    assert error_records[0]["extra"]["error_type"] == "RuntimeError"
    cast(AsyncMock, harness.session.flush).assert_called_once()


@pytest.mark.asyncio
async def test_no_active_alerts_updates_price_and_returns() -> None:
    product = _make_product(current_price=100000)
    market_data = MarketplaceProductData(name="Товар", price=90000)
    harness = _build_harness(product=product, market_data=market_data, alerts=[])

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    assert product.current_price == 90000
    assert product.previous_price == 100000
    harness.user_repo.get_by_id.assert_not_called()
    harness.notification.send_alert.assert_not_called()
    cast(AsyncMock, harness.session.flush).assert_called_once()


@pytest.mark.asyncio
async def test_user_missing_flushes_without_sending() -> None:
    product = _make_product(current_price=100000)
    alert = _make_alert(target_price=200000)
    market_data = MarketplaceProductData(name="Товар", price=90000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=[alert], user=None
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.notification.send_alert.assert_not_called()
    cast(AsyncMock, harness.session.flush).assert_called_once()


@pytest.mark.asyncio
async def test_repository_calls_do_not_grow_with_alert_count() -> None:
    """AC10: one query per repository per `check_product` call, regardless
    of how many active alerts a product has - no N+1 inside the alert loop."""
    product = _make_product(current_price=100000)
    alerts = [_make_alert(alert_id=i, target_price=200000) for i in range(1, 4)]
    market_data = MarketplaceProductData(name="Товар", price=100000)
    harness = _build_harness(
        product=product, market_data=market_data, alerts=alerts, user=_make_user()
    )

    await harness.service.check_product(1, cast(AsyncSession, harness.session))

    harness.product_repo.get_by_id.assert_called_once_with(1)
    harness.alert_repo.get_active_by_product.assert_called_once_with(product.id)
    harness.user_repo.get_by_id.assert_called_once_with(product.user_id)
    cast(AsyncMock, harness.session.flush).assert_called_once()
    assert harness.notification.send_alert.call_count == 3
