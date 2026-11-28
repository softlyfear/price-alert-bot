"""Receipt ordering of raw failures on migrated, file-backed SQLite."""

from collections.abc import AsyncIterator
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.services.price_service as price_service_module
from app.bot.texts import refresh_card_summary
from app.bot.texts import refresh_list_summary
from app.core.database import create_sqlite_engine
from app.handlers.product_card import card_screen
from app.models.alert import Alert
from app.models.enums import Marketplace
from app.models.enums import ProductFailure
from app.models.product import Product
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.schemas.marketplace import WbSizeData
from app.services.base_client import BaseMarketplaceClient
from app.services.notification import NotificationService
from app.services.price_service import PriceRefresher
from app.services.price_service import PriceService
from app.services.price_service import RefreshFailure
from app.services.price_service import RefreshLimiter
from app.services.price_service import RefreshReport
from app.services.tracking import TrackingService
from tests.integration.repositories.conftest import make_alert
from tests.integration.repositories.conftest import make_user

pytestmark = pytest.mark.asyncio

_FIRST = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
_SECOND = datetime(2026, 11, 19, 10, 1, tzinfo=UTC)
_THIRD = datetime(2026, 11, 19, 10, 2, tzinfo=UTC)
_CHECK = datetime(2026, 11, 19, 10, 3, tzinfo=UTC)
_LATE = datetime(2026, 11, 19, 10, 4, tzinfo=UTC)
_ARTICLE = 123
_TG_USER = 42


@dataclass
class _Clock:
    value: datetime = _FIRST

    def now(self, tz: object) -> datetime:
        assert tz is UTC
        return self.value


@dataclass
class _Run:
    sessions: async_sessionmaker[AsyncSession]
    service: PriceService
    client: MagicMock
    clock: _Clock
    tracking: Callable[[AsyncSession], TrackingService]

    async def fetch(
        self, when: datetime, result: MarketplaceFetchResult
    ) -> MarketplaceFetchResult:
        self.clock.value = when
        self.client.get_products_data.return_value = {_ARTICLE: result}
        return (await self.service.fetch_prices(Marketplace.wb, [_ARTICLE]))[_ARTICLE]

    async def apply(
        self, product_id: int, when: datetime, result: MarketplaceFetchResult
    ) -> FetchFailureReason | None:
        self.clock.value = when
        async with self.sessions() as session:
            reason = await self.service.apply_result(product_id, result, session)
            await session.commit()
        return reason

    async def stored(self, product_id: int, alert_id: int) -> Product:
        async with self.sessions() as session:
            product = await session.get(Product, product_id)
            alert = await session.get(Alert, alert_id)
            assert product is not None and alert is not None
            assert alert.triggered_at == _FIRST
            return product


@pytest_asyncio.fixture
async def run(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Run]:
    clock = _Clock()
    monkeypatch.setattr(price_service_module, "datetime", clock)
    engine = create_sqlite_engine(database_path)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    client = MagicMock(spec=BaseMarketplaceClient)
    client.batch_size = 100
    notification = AsyncMock(spec=NotificationService)
    try:
        async with httpx.AsyncClient() as http:
            service = PriceService(
                notification,
                ProductRepository,
                AlertRepository,
                UserRepository,
                lambda _market, _http: client,
                http,
                0,
            )
            yield _Run(
                sessions,
                service,
                client,
                clock,
                lambda session: TrackingService(
                    session, http, lambda _market, _http: client, 50
                ),
            )
            notification.send_alert.assert_not_awaited()
    finally:
        await engine.dispose()


async def _seed(run: _Run, stored_at: datetime | None) -> tuple[int, int]:
    async with run.sessions() as session, session.begin():
        user = await make_user(session, tg_user_id=_TG_USER)
        product = Product(
            user_id=user.id,
            marketplace=Marketplace.wb,
            article=_ARTICLE,
            product_name="Stored product",
            initial_price=9_500,
            current_price=10_000,
            previous_price=9_500,
            price_fetched_at=stored_at,
            last_checked_at=_CHECK,
            last_failure=ProductFailure.bad_payload,
            wb_size_id=7,
            wb_size_name="original size",
        )
        session.add(product)
        await session.flush()
        alert = await make_alert(
            session, user_id=user.id, product_id=product.id, target_price=10_500
        )
        alert.triggered_at = _FIRST
        return product.id, alert.id


def _in_stock() -> MarketplaceProductData:
    return MarketplaceProductData(
        name="New data",
        price=8_000,
        wb_sizes={7: WbSizeData(name="new size", price=11_000)},
    )


def _out_of_stock() -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(
        reason=FetchFailureReason.out_of_stock, detail="no price", product_name="Item"
    )


@pytest.mark.parametrize(
    ("failed_at", "expected_status", "expected_checked"),
    [
        pytest.param(_FIRST, None, _CHECK, id="older"),
        pytest.param(_SECOND, None, _CHECK, id="equal"),
        pytest.param(_THIRD, ProductFailure.out_of_stock, _LATE, id="newer"),
    ],
)
async def test_failure_applied_after_success_is_ordered_by_receipt(
    run: _Run,
    failed_at: datetime,
    expected_status: ProductFailure | None,
    expected_checked: datetime,
) -> None:
    product_id, alert_id = await _seed(run, None)
    # Even the genuinely newer failure was received before the success was applied.
    if failed_at <= _SECOND:
        failure = await run.fetch(failed_at, _out_of_stock())
        success = await run.fetch(_SECOND, _in_stock())
    else:
        success = await run.fetch(_SECOND, _in_stock())
        failure = await run.fetch(failed_at, _out_of_stock())
    assert await run.apply(product_id, _CHECK, success) is None
    assert (
        await run.apply(product_id, _LATE, failure) is FetchFailureReason.out_of_stock
    )

    saved = await run.stored(product_id, alert_id)
    assert saved.last_failure is expected_status
    assert saved.last_checked_at == expected_checked
    assert (saved.current_price, saved.previous_price, saved.initial_price) == (
        11_000,
        10_000,
        9_500,
    )
    assert saved.price_fetched_at == _SECOND
    assert (saved.wb_size_id, saved.wb_size_name) == (7, "new size")
    assert saved.product_name == "Stored product"


@pytest.mark.parametrize(
    ("stored_at", "received_at"),
    [
        pytest.param(None, _FIRST, id="null-stored"),
        pytest.param(_SECOND, None, id="unstamped"),
        pytest.param(None, None, id="both-missing"),
    ],
)
async def test_missing_receipt_does_not_prevent_status_write(
    run: _Run, stored_at: datetime | None, received_at: datetime | None
) -> None:
    product_id, alert_id = await _seed(run, stored_at)
    failure: MarketplaceFetchResult = _out_of_stock()
    if received_at is not None:
        failure = await run.fetch(received_at, failure)
    assert (
        await run.apply(product_id, _LATE, failure) is FetchFailureReason.out_of_stock
    )
    saved = await run.stored(product_id, alert_id)
    assert saved.last_failure is ProductFailure.out_of_stock
    assert saved.last_checked_at == _LATE
    assert saved.price_fetched_at == stored_at
    assert (saved.current_price, saved.previous_price, saved.initial_price) == (
        10_000,
        9_500,
        9_500,
    )
    assert (saved.wb_size_id, saved.wb_size_name) == (7, "original size")


async def test_failures_still_follow_apply_order_and_do_not_watermark_success(
    run: _Run,
) -> None:
    product_id, alert_id = await _seed(run, _FIRST)
    earlier = await run.fetch(
        _SECOND, MarketplaceFetchFailure(reason=FetchFailureReason.transport_error)
    )
    success = await run.fetch(_SECOND, _in_stock())
    later = await run.fetch(_THIRD, _out_of_stock())

    assert await run.apply(product_id, _CHECK, later) is FetchFailureReason.out_of_stock
    assert (await run.stored(product_id, alert_id)).last_failure is (
        ProductFailure.out_of_stock
    )
    assert (
        await run.apply(product_id, _LATE, earlier)
        is FetchFailureReason.transport_error
    )
    failed = await run.stored(product_id, alert_id)
    assert failed.last_failure is ProductFailure.transport_error
    assert failed.last_checked_at == _LATE
    assert failed.price_fetched_at == _FIRST

    assert await run.apply(product_id, _LATE, success) is None
    saved = await run.stored(product_id, alert_id)
    assert saved.last_failure is None
    assert saved.price_fetched_at == _SECOND
    assert saved.current_price == 11_000


async def test_newer_wb_observation_without_selected_size_still_writes_failure(
    run: _Run,
) -> None:
    product_id, alert_id = await _seed(run, _SECOND)
    result = await run.fetch(
        _THIRD,
        MarketplaceProductData(
            name="Item",
            price=8_000,
            wb_sizes={8: WbSizeData(name="other", price=8_000)},
        ),
    )
    assert await run.apply(product_id, _LATE, result) is FetchFailureReason.out_of_stock
    saved = await run.stored(product_id, alert_id)
    assert saved.last_failure is ProductFailure.out_of_stock
    assert saved.last_checked_at == _LATE
    assert saved.price_fetched_at == _SECOND
    assert (saved.current_price, saved.previous_price) == (10_000, 9_500)
    assert (saved.wb_size_id, saved.wb_size_name) == (7, "original size")


@pytest.mark.parametrize("refresh_all", [False, True], ids=["card", "list"])
async def test_manual_refresh_reports_older_failure_but_preserves_preview_price(
    run: _Run, refresh_all: bool
) -> None:
    async with run.sessions() as session:
        tracking = run.tracking(session)
        alert = await tracking.add_tracking(
            _TG_USER,
            Marketplace.wb,
            _ARTICLE,
            "Preview item",
            11_000,
            9_000,
            price_fetched_at=_SECOND,
        )
        if refresh_all:
            await tracking.add_tracking(
                _TG_USER,
                Marketplace.wb,
                456,
                "Other item",
                11_000,
                9_000,
                price_fetched_at=None,
            )
        product_id = alert.product_id
        product = await session.get(Product, product_id)
        assert product is not None
        previous_check = product.last_checked_at
        await session.commit()
    run.clock.value = _FIRST
    run.client.get_products_data.return_value = (
        {
            _ARTICLE: _out_of_stock(),
            456: MarketplaceProductData(name="Other", price=12_000),
        }
        if refresh_all
        else {_ARTICLE: _out_of_stock()}
    )
    service = PriceRefresher(
        run.service,
        run.sessions,
        ProductRepository,
        UserRepository,
        concurrency=1,
        limiter=RefreshLimiter(60.0, lambda: 0.0),
        deadline_seconds=20.0,
    )
    if refresh_all:
        report = await service.refresh_list(_TG_USER)
        assert report == RefreshReport(2, 1, {RefreshFailure.out_of_stock: 1})
        assert refresh_list_summary(report) == "Обновлено 1 из 2.\nНет в наличии: 1"
    else:
        card_report = await service.refresh_product(_TG_USER, product_id)
        assert isinstance(card_report, RefreshReport)
        assert card_report == RefreshReport(1, 0, {RefreshFailure.out_of_stock: 1})
        assert refresh_card_summary(card_report, Marketplace.wb) == (
            "Товара сейчас нет в наличии. Показана прежняя цена."
        )

    async with run.sessions() as session:
        card = await run.tracking(session).get_product_card(_TG_USER, product_id)
        assert card is not None
        assert card.product.last_failure is None
        assert card.product.last_checked_at == previous_check
        assert card.product.price_fetched_at == _SECOND
        text, _ = card_screen(card, ZoneInfo("UTC"))
        assert "Текущая цена: 110\u00a0₽" in text
        assert "Нет в наличии" not in text
