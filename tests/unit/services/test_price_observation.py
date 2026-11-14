"""Successful-observation ordering at the service boundary."""

from datetime import UTC
from datetime import datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.price_service as price_service_module
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from tests.unit.services.test_price_service import _build_harness
from tests.unit.services.test_price_service import _FrozenClock
from tests.unit.services.test_price_service import _make_alert
from tests.unit.services.test_price_service import _make_product
from tests.unit.services.test_price_service import _make_user

_PREVIEW = datetime(2026, 11, 14, 10, 1, tzinfo=UTC)
_OLD = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 14, 10, 2, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fetch_timestamps_the_completion_not_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(_OLD))
    result = MarketplaceProductData(name="Item", price=10_000)
    harness = _build_harness(product=None, market_data=result)

    async def client_reply(articles: list[int]) -> dict[int, MarketplaceProductData]:
        assert articles == [123]
        monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(_NOW))
        return {123: result}

    harness.client.get_products_data.side_effect = client_reply
    fetched = (await harness.service.fetch_prices(Marketplace.wb, [123]))[123]
    assert isinstance(fetched, MarketplaceProductData)
    assert fetched.fetched_at == _NOW
    assert result.fetched_at is None


@pytest.mark.asyncio
async def test_cooldown_uses_application_time_rather_than_receipt_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(_NOW))
    product = _make_product(current_price=10_000)
    alert = _make_alert(
        target_price=9_000, triggered_at=datetime(2026, 11, 14, 9, 1, tzinfo=UTC)
    )
    result = MarketplaceProductData(name="Item", price=8_000, fetched_at=_OLD)
    user = _make_user()
    harness = _build_harness(
        product=product, market_data=result, alerts=[alert], user=user
    )

    await harness.service.apply_result(1, result, cast(AsyncSession, harness.session))
    harness.notification.send_alert.assert_awaited_once_with(
        alert=alert, product=product, user=user
    )
    assert product.price_fetched_at == _OLD
    assert product.last_checked_at == _NOW


@pytest.mark.asyncio
async def test_preview_observation_blocks_an_older_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(_NOW))
    product = _make_product(current_price=11_000, previous_price=10_000)
    product.price_fetched_at = _PREVIEW
    product.last_checked_at = _PREVIEW
    result = MarketplaceProductData(name="Old item", price=8_000, fetched_at=_OLD)
    harness = _build_harness(product=product, market_data=result)

    assert (
        await harness.service.apply_result(
            1, result, cast(AsyncSession, harness.session)
        )
        is None
    )
    assert (product.current_price, product.previous_price) == (11_000, 10_000)
    assert (product.price_fetched_at, product.last_checked_at) == (_PREVIEW, _PREVIEW)
    harness.session.flush.assert_not_awaited()
    harness.notification.send_alert.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", [None, _OLD], ids=["unstamped", "observed"])
async def test_first_or_unstamped_success_retains_compatible_time(
    monkeypatch: pytest.MonkeyPatch, stamp: datetime | None
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(_NOW))
    product = _make_product(current_price=10_000)
    result = MarketplaceProductData(name="Item", price=11_000, fetched_at=stamp)
    harness = _build_harness(product=product, market_data=result)

    await harness.service.apply_result(1, result, cast(AsyncSession, harness.session))
    assert product.current_price == 11_000
    assert product.previous_price == 10_000
    assert product.price_fetched_at == (stamp if stamp is not None else _NOW)
    assert product.last_checked_at == _NOW


@pytest.mark.asyncio
async def test_failure_does_not_block_success_observed_before_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenClock(_NOW))
    product = _make_product(current_price=10_000)
    product.price_fetched_at = datetime(2026, 11, 14, 9, 59, tzinfo=UTC)
    failure = MarketplaceFetchFailure(reason=FetchFailureReason.blocked)
    harness = _build_harness(product=product, market_data=failure)
    session = cast(AsyncSession, harness.session)

    assert (
        await harness.service.apply_result(1, failure, session)
        is FetchFailureReason.blocked
    )
    assert product.current_price == 10_000
    assert product.price_fetched_at == datetime(2026, 11, 14, 9, 59, tzinfo=UTC)
    assert product.last_checked_at == _NOW
    success = MarketplaceProductData(name="Item", price=11_000, fetched_at=_OLD)
    assert await harness.service.apply_result(1, success, session) is None
    assert product.current_price == 11_000
    assert product.price_fetched_at == _OLD
    assert product.last_checked_at == _NOW
