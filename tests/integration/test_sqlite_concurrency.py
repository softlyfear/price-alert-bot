"""Concurrent operations use independent connections to a real SQLite file."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import create_sqlite_engine
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import ProductLimitExceededError
from app.models.alert import Alert
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.repositories.user import UserRepository
from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient
from app.services.tracking import TrackingService


def _tracking(
    session: AsyncSession,
    *,
    client: BaseMarketplaceClient | None = None,
    limit: int = 50,
) -> TrackingService:
    return TrackingService(
        session=session,
        http_client=cast(httpx.AsyncClient, MagicMock()),
        client_factory=lambda _marketplace, _http: cast(BaseMarketplaceClient, client),
        max_products_per_user=limit,
    )


@pytest.mark.asyncio
async def test_two_sessions_create_same_user_once(database_path: Path) -> None:
    engine = create_sqlite_engine(database_path, busy_timeout_seconds=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    connections: set[int] = set()

    @event.listens_for(engine.sync_engine, "checkout")
    def _record(dbapi_connection: object, _record: object, _proxy: object) -> None:
        connections.add(id(dbapi_connection))

    async def create() -> tuple[int, bool]:
        async with factory() as session:
            user, created = await UserRepository(session).get_or_create_by_tg_id(123)
            result = user.id, created
            await session.commit()
            return result

    try:
        first, second = await asyncio.gather(create(), create())
        assert first[0] == second[0]
        assert sorted([first[1], second[1]]) == [False, True]
        assert len(connections) >= 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_article", [False, True])
async def test_concurrent_additions_keep_limit_and_unique_alerts(
    database_path: Path, same_article: bool
) -> None:
    engine = create_sqlite_engine(database_path, busy_timeout_seconds=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    connections: set[int] = set()

    @event.listens_for(engine.sync_engine, "checkout")
    def _record(dbapi_connection: object, _record: object, _proxy: object) -> None:
        connections.add(id(dbapi_connection))

    async def add(article: int) -> str:
        async with factory() as session:
            try:
                await _tracking(session, limit=1).add_tracking(
                    123, Marketplace.wb, article, "test", 100, 50, price_fetched_at=None
                )
                await session.commit()
                return "added"
            except (ProductLimitExceededError, DuplicateAlertError) as exc:
                await session.rollback()
                return type(exc).__name__

    try:
        results = await asyncio.gather(add(1), add(1 if same_article else 2))
        assert sorted(results) == sorted(
            [
                "added",
                "DuplicateAlertError" if same_article else "ProductLimitExceededError",
            ]
        )
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(Product)) == 1
            assert await session.scalar(select(func.count()).select_from(Alert)) == 1
        assert len(connections) >= 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_waiting_preview_does_not_block_other_user_writer(
    database_path: Path, existing: bool
) -> None:
    engine = create_sqlite_engine(database_path, busy_timeout_seconds=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    connections: set[int] = set()

    @event.listens_for(engine.sync_engine, "checkout")
    def _record(dbapi_connection: object, _record: object, _proxy: object) -> None:
        connections.add(id(dbapi_connection))

    class Client(BaseMarketplaceClient):
        async def get_product_data(self, article: int) -> MarketplaceProductData:
            entered.set()
            await release.wait()
            return MarketplaceProductData(name="example", price=100)

    async def preview() -> MarketplaceProductData:
        async with factory() as session:
            result = await _tracking(session, client=Client()).preview(
                123, Marketplace.wb, 1
            )
            assert isinstance(result, MarketplaceProductData)
            return result

    try:
        if existing:
            async with factory() as session:
                await UserRepository(session).get_or_create_by_tg_id(123)
                await session.commit()
        task = asyncio.create_task(preview())
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            async with factory() as session:
                await _tracking(session).add_tracking(
                    456, Marketplace.wb, 2, "other", 100, 50, price_fetched_at=None
                )
                await asyncio.wait_for(session.commit(), timeout=3)
            assert not task.done()
            assert len(connections) >= 2
        finally:
            release.set()
            await task
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(User).where(User.tg_user_id == 456)
                )
                == 1
            )
            assert await session.scalar(
                select(func.count()).select_from(User).where(User.tg_user_id == 123)
            ) == int(existing)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_write_boundary_does_not_end_existing_read_transaction(
    database_path: Path,
) -> None:
    engine = create_sqlite_engine(database_path, busy_timeout_seconds=1.25)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repository = UserRepository(session)
            await repository.begin_write()
            await repository.begin_write()
            user, _ = await repository.get_or_create_by_tg_id(10)
            assert user.tg_user_id == 10
            await session.commit()
        async with factory() as session:
            repository = UserRepository(session)
            reloaded = await repository.get_by_tg_id(10)
            assert reloaded is not None
            with pytest.raises(RuntimeError, match="before any reads"):
                await repository.begin_write()
            assert session.in_transaction()
            assert await repository.get_by_tg_id(10) is reloaded
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_user_preview_article_and_page_do_not_create_rows(
    database_path: Path,
) -> None:
    engine = create_sqlite_engine(database_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class Client(BaseMarketplaceClient):
        async def get_product_data(self, article: int) -> MarketplaceProductData:
            return MarketplaceProductData(name="example", price=100)

    try:
        async with factory() as session:
            service = _tracking(session, client=Client())
            result = await service.preview_article(123, 55)
            assert result.marketplace == Marketplace.wb
            assert await service.list_page_of(123, 99) == 1
            assert await session.scalar(select(func.count()).select_from(User)) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_manual_refresh_and_other_user_write_concurrently(
    database_path: Path,
) -> None:
    from collections.abc import Sequence
    from time import monotonic

    from loguru import logger

    from app.repositories.alert import AlertRepository
    from app.repositories.product import ProductRepository
    from app.scheduler import run_price_check_pass
    from app.schemas.marketplace import MarketplaceFetchResult
    from app.services.notification import NotificationService
    from app.services.price_service import PriceRefresher
    from app.services.price_service import PriceService
    from app.services.price_service import RefreshLimiter
    from app.services.price_service import RefreshReport

    engine = create_sqlite_engine(database_path, busy_timeout_seconds=5)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    both_fetching, release = asyncio.Event(), asyncio.Event()
    fetch_count = 0

    class Client(BaseMarketplaceClient):
        batch_size = 100

        async def get_product_data(self, article: int) -> MarketplaceProductData:
            return MarketplaceProductData(name="new", price=90)

        async def get_products_data(
            self, articles: Sequence[int]
        ) -> dict[int, MarketplaceFetchResult]:
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 2:
                both_fetching.set()
            await release.wait()
            return {
                article: MarketplaceProductData(name="new", price=90)
                for article in articles
            }

    client = Client()
    service = PriceService(
        notification_service=cast(NotificationService, MagicMock()),
        product_repo_factory=ProductRepository,
        alert_repo_factory=AlertRepository,
        user_repo_factory=UserRepository,
        client_factory=lambda _marketplace, _http: client,
        http_client=cast(httpx.AsyncClient, MagicMock()),
        alert_cooldown_seconds=3600,
    )
    refresher = PriceRefresher(
        service,
        factory,
        ProductRepository,
        UserRepository,
        concurrency=2,
        limiter=RefreshLimiter(60, monotonic),
        deadline_seconds=10,
    )
    errors: list[str] = []
    sink = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        async with factory() as session:
            await _tracking(session).add_tracking(
                123, Marketplace.wb, 1, "old", 100, 50, price_fetched_at=None
            )
            await session.commit()
        scheduled = asyncio.create_task(
            run_price_check_pass(
                service, factory, concurrency=2, stop_event=asyncio.Event()
            )
        )
        manual = asyncio.create_task(refresher.refresh_list(123))
        try:
            await asyncio.wait_for(both_fetching.wait(), timeout=5)
            async with factory() as session:
                await _tracking(session).add_tracking(
                    456, Marketplace.wb, 2, "other", 200, 50, price_fetched_at=None
                )
                await asyncio.wait_for(session.commit(), timeout=5)
        finally:
            release.set()
        await asyncio.wait_for(scheduled, timeout=10)
        assert await asyncio.wait_for(manual, timeout=10) == RefreshReport(
            total=1, updated=1, failures={}
        )
        async with factory() as session:
            products = (
                await session.scalars(select(Product).order_by(Product.article))
            ).all()
            assert [(p.article, p.current_price) for p in products] == [
                (1, 90),
                (2, 200),
            ]
        assert errors == []
    finally:
        logger.remove(sink)
        await engine.dispose()
