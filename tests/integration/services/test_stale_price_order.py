"""Out-of-order fetched results cannot roll back a newer observation."""

import asyncio
from datetime import UTC
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.services.price_service as price_service_module
from app.core.database import create_sqlite_engine
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceProductData
from app.schemas.marketplace import WbSizeData
from app.services.base_client import BaseMarketplaceClient
from app.services.notification import NotificationService
from app.services.price_service import PriceRefresher
from app.services.price_service import PriceService
from app.services.price_service import RefreshLimiter
from app.services.price_service import RefreshReport
from tests.integration.repositories.conftest import make_alert
from tests.integration.repositories.conftest import make_user

_OLD = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
_NEW = datetime(2026, 11, 14, 10, 1, tzinfo=UTC)
_APPLY = datetime(2026, 11, 14, 10, 2, tzinfo=UTC)
_LATE = datetime(2026, 11, 14, 10, 3, tzinfo=UTC)


class _Clock:
    value = _OLD

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return cls.value


@pytest.mark.asyncio
@pytest.mark.parametrize("same_timestamp", [False, True], ids=["older", "equal"])
@pytest.mark.parametrize("missing_size", [False, True], ids=["size", "missing-size"])
async def test_older_fetch_applied_after_newer_success_is_a_noop(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_timestamp: bool,
    missing_size: bool,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _Clock)
    engine = create_sqlite_engine(database_path)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    notification = AsyncMock(spec=NotificationService)
    client = MagicMock(spec=BaseMarketplaceClient)
    client.get_products_data.side_effect = [
        {
            123: MarketplaceProductData(
                name="old",
                price=8_000,
                wb_sizes={}
                if missing_size
                else {7: WbSizeData(name="old size", price=8_000)},
            )
        },
        {
            123: MarketplaceProductData(
                name="new",
                price=11_000,
                wb_sizes={7: WbSizeData(name="new size", price=11_000)},
            )
        },
    ]
    try:
        async with factory() as session, session.begin():
            user = await make_user(session)
            product = Product(
                user_id=user.id,
                marketplace=Marketplace.wb,
                article=123,
                product_name="Stored product",
                current_price=10_000,
                wb_size_id=7,
                wb_size_name="original size",
            )
            session.add(product)
            await session.flush()
            alert = await make_alert(
                session,
                user_id=user.id,
                product_id=product.id,
                target_price=9_000,
                direction=AlertDirection.below,
            )
            product_id, alert_id = product.id, alert.id

        async with httpx.AsyncClient() as http:
            service = PriceService(
                notification,
                ProductRepository,
                AlertRepository,
                UserRepository,
                lambda _market, _http: client,
                http,
                3600,
            )
            _Clock.value = _OLD
            received, release = asyncio.Event(), asyncio.Event()

            async def delayed_old() -> FetchFailureReason | None:
                older = (await service.fetch_prices(Marketplace.wb, [123]))[123]
                received.set()
                await release.wait()
                async with factory() as session:
                    outcome = await service.apply_result(product_id, older, session)
                    await session.commit()
                    return outcome

            task = asyncio.create_task(delayed_old())
            try:
                async with asyncio.timeout(5):
                    await received.wait()
                    _Clock.value = _OLD if same_timestamp else _NEW
                    newer = (await service.fetch_prices(Marketplace.wb, [123]))[123]
                    _Clock.value = _APPLY
                    async with factory() as session:
                        await service.apply_result(product_id, newer, session)
                        await session.commit()
                    _Clock.value = _LATE
                    release.set()
                    assert await task is None
            finally:
                cancelled_by_cleanup = not task.done()
                if cancelled_by_cleanup:
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if not cancelled_by_cleanup or (
                        current is not None and current.cancelling()
                    ):
                        raise

        async with factory() as session:
            persisted = await session.get(Product, product_id)
            saved_alert = await session.get(Alert, alert_id)
            assert persisted is not None and saved_alert is not None
            assert persisted.current_price == 11_000
            assert persisted.previous_price == 10_000
            assert persisted.product_name == "Stored product"
            assert persisted.wb_size_name == "new size"
            assert persisted.price_fetched_at == (_OLD if same_timestamp else _NEW)
            assert persisted.last_checked_at == _APPLY
            assert saved_alert.triggered_at is None
        notification.send_alert.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_refresh_counts_a_skipped_success_as_updated(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _Clock)
    _Clock.value = _OLD
    engine = create_sqlite_engine(database_path)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    notification = AsyncMock(spec=NotificationService)
    client = MagicMock(spec=BaseMarketplaceClient)
    client.batch_size = 100
    client.get_products_data.return_value = {
        123: MarketplaceProductData(name="Old item", price=8_000)
    }
    try:
        async with factory() as session, session.begin():
            user = await make_user(session, tg_user_id=42)
            product = Product(
                user_id=user.id,
                marketplace=Marketplace.wb,
                article=123,
                product_name="Preview",
                current_price=11_000,
                previous_price=10_000,
                price_fetched_at=_NEW,
                last_checked_at=_NEW,
            )
            session.add(product)
            await session.flush()
            product_id = product.id
            await make_alert(
                session, user_id=user.id, product_id=product_id, target_price=9_000
            )

        async with httpx.AsyncClient() as http:
            service = PriceService(
                notification,
                ProductRepository,
                AlertRepository,
                UserRepository,
                lambda _market, _http: client,
                http,
                3600,
            )
            refresher = PriceRefresher(
                service,
                factory,
                ProductRepository,
                UserRepository,
                concurrency=1,
                limiter=RefreshLimiter(60.0, lambda: 0.0),
                deadline_seconds=20.0,
            )
            report = await refresher.refresh_product(42, product_id)

        assert report == RefreshReport(total=1, updated=1, failures={})
        async with factory() as session:
            saved = await session.get(Product, product_id)
            assert saved is not None
            assert (saved.current_price, saved.previous_price) == (11_000, 10_000)
            assert (saved.price_fetched_at, saved.last_checked_at) == (_NEW, _NEW)
        notification.send_alert.assert_not_awaited()
    finally:
        await engine.dispose()
