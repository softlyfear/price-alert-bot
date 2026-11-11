"""Size identities and per-product projection on migrated SQLite."""

from datetime import UTC
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import InvalidPatchFieldsError
from app.domain.exceptions import ProductLimitExceededError
from app.models.enums import Marketplace
from app.models.product import Product
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceProductData
from app.schemas.marketplace import WbSizeData
from app.services.price_service import PriceService
from app.services.tracking import TrackingService
from tests.integration.services.test_tracking_service_live import _CountingClient

pytestmark = pytest.mark.asyncio


def _tracking(session: AsyncSession, limit: int = 50) -> TrackingService:
    return TrackingService(
        session, httpx.AsyncClient(), lambda _m, _h: _CountingClient(), limit
    )


async def test_full_identity_reuse_limit_and_user_isolation(
    db_session: AsyncSession,
) -> None:
    service = _tracking(db_session, limit=3)
    alerts = []
    for size in (0, 42, 43):
        alerts.append(
            await service.add_tracking(
                100,
                Marketplace.wb,
                123,
                "Item",
                1000,
                900,
                price_fetched_at=None,
                wb_size_id=size,
                wb_size_name="42" if size == 42 else None,
            )
        )
    other = await service.add_tracking(
        200,
        Marketplace.wb,
        123,
        "Item",
        1000,
        900,
        price_fetched_at=None,
        wb_size_id=42,
    )
    assert len({a.product_id for a in [*alerts, other]}) == 4
    again = await service.add_tracking(
        100,
        Marketplace.wb,
        123,
        "Item",
        1000,
        800,
        price_fetched_at=None,
        wb_size_id=42,
    )
    assert again.product_id == alerts[1].product_id
    for size, alert in zip((0, 42, 43), alerts, strict=True):
        card = await service.find_tracked_product(
            100, Marketplace.wb, 123, wb_size_id=size
        )
        assert card is not None and card.product.id == alert.product_id
    with pytest.raises(ProductLimitExceededError):
        await service.add_tracking(
            100,
            Marketplace.wb,
            123,
            "Item",
            1000,
            700,
            price_fetched_at=None,
            wb_size_id=44,
        )
    assert await service.get_product_card(200, alerts[1].product_id) is None


async def test_duplicate_identity_constraint_and_immutable_patches(
    db_session: AsyncSession,
) -> None:
    service = _tracking(db_session)
    alert = await service.add_tracking(
        100,
        Marketplace.wb,
        123,
        "Item",
        1000,
        900,
        price_fetched_at=None,
        wb_size_id=42,
    )
    product = await db_session.get(Product, alert.product_id)
    assert product is not None
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Product(
                    user_id=product.user_id,
                    marketplace=Marketplace.wb,
                    article=123,
                    wb_size_id=42,
                    product_name="Item",
                    current_price=1000,
                )
            )
            await db_session.flush()
    repo = ProductRepository(db_session)
    with pytest.raises(InvalidPatchFieldsError):
        await repo.patch(product.id, {"wb_size_id": 43, "wb_size_name": "43"})
    assert (product.wb_size_id, product.wb_size_name) == (42, None)


@pytest.mark.parametrize("reverse", [False, True])
async def test_shared_article_applies_independent_prices_and_stock_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    import app.services.price_service as price_module

    now = datetime(2026, 1, 3, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(tz: object = None) -> datetime:
            return now

    monkeypatch.setattr(price_module, "datetime", Clock)
    service = _tracking(db_session)
    alerts = []
    for tg, size in ((100, 0), (100, 42), (200, 43)):
        alerts.append(
            await service.add_tracking(
                tg,
                Marketplace.wb,
                123,
                "Item",
                1000,
                100,
                price_fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                wb_size_id=size,
                wb_size_name="old" if size else None,
            )
        )
    snapshot = MarketplaceProductData(
        name="Item",
        price=800,
        wb_sizes={
            42: WbSizeData(name="new", price=1200),
            43: WbSizeData(name="43", price=None),
        },
    )
    original = snapshot.model_dump()
    notification: Any = AsyncMock()
    price = PriceService(
        notification,
        ProductRepository,
        AlertRepository,
        UserRepository,
        lambda _m, _h: _CountingClient(),
        httpx.AsyncClient(),
        3600,
    )
    ordered = list(reversed(alerts)) if reverse else alerts
    outcomes = {}
    for alert in ordered:
        outcomes[alert.product_id] = await price.apply_result(
            alert.product_id, snapshot, db_session
        )
    rows = [await db_session.get(Product, a.product_id) for a in alerts]
    assert all(row is not None for row in rows)
    legacy, selected, missing = rows
    assert legacy is not None and selected is not None and missing is not None
    assert (legacy.current_price, selected.current_price, selected.wb_size_name) == (
        800,
        1200,
        "new",
    )
    assert (
        missing.current_price,
        missing.previous_price,
        missing.initial_price,
        missing.wb_size_name,
    ) == (1000, None, 1000, "old")
    assert missing.price_fetched_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert missing.last_checked_at == now
    assert outcomes[missing.id] == FetchFailureReason.out_of_stock
    assert snapshot.model_dump() == original
    notification.send_alert.assert_not_awaited()


async def test_racing_insert_reuses_exact_size_not_sibling_or_other_user(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.services.test_tracking_service_live import (
        _first_call_sees_nothing,
    )

    service = _tracking(db_session)
    sibling = await service.add_tracking(
        100,
        Marketplace.wb,
        123,
        "Item",
        1000,
        900,
        price_fetched_at=None,
        wb_size_id=43,
    )
    winner = await service.add_tracking(
        100,
        Marketplace.wb,
        123,
        "Item",
        1000,
        900,
        price_fetched_at=None,
        wb_size_id=42,
    )
    foreign = await service.add_tracking(
        200,
        Marketplace.wb,
        123,
        "Item",
        1000,
        900,
        price_fetched_at=None,
        wb_size_id=42,
    )
    await _first_call_sees_nothing(
        monkeypatch, ProductRepository, "get_by_article_and_user", None
    )
    loser = await service.add_tracking(
        100,
        Marketplace.wb,
        123,
        "Item",
        1000,
        800,
        price_fetched_at=None,
        wb_size_id=42,
    )
    assert loser.product_id == winner.product_id
    assert loser.product_id not in {sibling.product_id, foreign.product_id}
