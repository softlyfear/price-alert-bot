"""Unit tests for ``TrackingService.preview_article`` / ``find_tracked_article``.

PAB-113, PROJECT.md 2.15 R21. Mocked boundaries: the repository layer and the
marketplace client factory (one fake client per marketplace, each counting its
calls). Nothing touches a database or the network. The SQL and the statement
count of ``find_tracked_article`` live in
``tests/integration/services/test_tracking_find_article_live.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.tracking as tracking_module
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.links import BARE_ARTICLE_MARKETPLACES
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
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.tracking import ArticlePreview
from app.services.tracking import ProductCard
from app.services.tracking import TrackingService

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_TG = 555
_USER_ID = 77  # deliberately different from the Telegram id
_ARTICLE = 5_764_570_826
_LIMIT = 3
_WB = Marketplace.wb
_OZON = Marketplace.ozon
_WB_DATA = MarketplaceProductData(name="WB item", price=10_000)
_OZON_DATA = MarketplaceProductData(name="Ozon item", price=24_100)


def _failure(reason: FetchFailureReason) -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(reason=reason, detail="SECRET-DETAIL-MARKER")


class _Fakes:
    """Per-marketplace fake clients behind one factory, counting every call."""

    def __init__(self, wb: MarketplaceFetchResult, ozon: MarketplaceFetchResult):
        self.calls: list[tuple[Marketplace, int]] = []
        self._results = {_WB: wb, _OZON: ozon}
        self.unsupported: Marketplace | None = None

    def factory(
        self, marketplace: Marketplace, _http: httpx.AsyncClient
    ) -> BaseMarketplaceClient:
        if marketplace is self.unsupported:
            raise UnsupportedMarketplaceError(str(marketplace))
        fakes = self
        result = self._results[marketplace]

        class _Client(BaseMarketplaceClient):
            async def get_product_data(self, article: int) -> MarketplaceFetchResult:
                fakes.calls.append((marketplace, article))
                return result

        return cast(BaseMarketplaceClient, _Client.__new__(_Client))

    def count(self, marketplace: Marketplace) -> int:
        return sum(1 for m, _ in self.calls if m is marketplace)


class _Harness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fakes: _Fakes,
        *,
        active_count: int = 0,
        tracked: dict[Marketplace, Product] | None = None,
        alerts: list[Alert] | None = None,
    ) -> None:
        self.fakes = fakes
        tracked = tracked or {}
        self.users = MagicMock(spec=UserRepository)
        self.users.get_or_create_by_tg_id.return_value = (
            User(id=_USER_ID, tg_user_id=_TG),
            False,
        )
        self.products = MagicMock(spec=ProductRepository)
        self.products.count_active_by_user.return_value = active_count

        async def by_article(
            article: int, user_id: int, marketplace: Marketplace
        ) -> Product | None:
            product = tracked.get(marketplace)
            if product is None or user_id != _USER_ID or article != _ARTICLE:
                return None
            return product

        self.products.get_by_article_and_user.side_effect = by_article
        self.products.count_before.return_value = 0
        self.alerts = MagicMock(spec=AlertRepository)
        self.alerts.get_by_user_and_product.return_value = alerts or []
        monkeypatch.setattr(tracking_module, "UserRepository", lambda _s: self.users)
        monkeypatch.setattr(
            tracking_module, "ProductRepository", lambda _s: self.products
        )
        monkeypatch.setattr(tracking_module, "AlertRepository", lambda _s: self.alerts)
        self.service = TrackingService(
            session=cast(AsyncSession, MagicMock(spec=AsyncSession)),
            http_client=httpx.AsyncClient(),
            client_factory=fakes.factory,
            max_products_per_user=_LIMIT,
        )


def _product(marketplace: Marketplace, product_id: int = 10) -> Product:
    return Product(
        id=product_id,
        user_id=_USER_ID,
        marketplace=marketplace,
        article=_ARTICLE,
        product_name="P",
        current_price=100,
    )


def _active_alert(product_id: int) -> Alert:
    return Alert(
        id=1,
        user_id=_USER_ID,
        product_id=product_id,
        target_price=50,
        direction=AlertDirection.below,
        is_active=True,
    )


@contextmanager
def _records() -> Iterator[list[Record]]:
    records: list[Record] = []

    def sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(sink, level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def test_candidate_order_is_wildberries_then_ozon() -> None:
    assert (Marketplace.wb, Marketplace.ozon) == BARE_ARTICLE_MARKETPLACES


# --- preview_article: routing matrix ---------------------------------------


@pytest.mark.asyncio
async def test_wb_data_wins_and_ozon_is_never_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(_WB_DATA, _OZON_DATA)
    h = _Harness(monkeypatch, fakes)

    result = await h.service.preview_article(_TG, _ARTICLE)

    assert result == ArticlePreview(_WB, _WB_DATA)
    assert fakes.calls == [(_WB, _ARTICLE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ozon_result",
    [
        _OZON_DATA,
        _failure(FetchFailureReason.not_found),
        _failure(FetchFailureReason.blocked),
        _failure(FetchFailureReason.transport_error),
        _failure(FetchFailureReason.bad_payload),
    ],
    ids=["data", "not_found", "blocked", "transport_error", "bad_payload"],
)
async def test_wb_not_found_asks_ozon_exactly_once_and_returns_its_outcome(
    monkeypatch: pytest.MonkeyPatch, ozon_result: MarketplaceFetchResult
) -> None:
    fakes = _Fakes(_failure(FetchFailureReason.not_found), ozon_result)
    h = _Harness(monkeypatch, fakes)

    result = await h.service.preview_article(_TG, _ARTICLE)

    assert result == ArticlePreview(_OZON, ozon_result)
    assert fakes.calls == [(_WB, _ARTICLE), (_OZON, _ARTICLE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        FetchFailureReason.blocked,
        FetchFailureReason.transport_error,
        FetchFailureReason.bad_payload,
        FetchFailureReason.out_of_stock,
    ],
)
async def test_other_wb_failure_is_returned_and_ozon_is_never_asked(
    monkeypatch: pytest.MonkeyPatch, reason: FetchFailureReason
) -> None:
    wb_failure = _failure(reason)
    fakes = _Fakes(wb_failure, _OZON_DATA)
    h = _Harness(monkeypatch, fakes)

    result = await h.service.preview_article(_TG, _ARTICLE)

    assert result == ArticlePreview(_WB, wb_failure)
    assert fakes.count(_OZON) == 0


# --- preview_article: limit -------------------------------------------------


@pytest.mark.asyncio
async def test_exhausted_limit_without_a_counted_product_raises_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(_WB_DATA, _OZON_DATA)
    h = _Harness(monkeypatch, fakes, active_count=_LIMIT)

    with pytest.raises(ProductLimitExceededError) as caught:
        await h.service.preview_article(_TG, _ARTICLE)

    assert caught.value.limit == _LIMIT
    assert fakes.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("counted", [_OZON, _WB])
async def test_exhausted_limit_does_not_block_an_article_counted_on_any_candidate(
    monkeypatch: pytest.MonkeyPatch, counted: Marketplace
) -> None:
    fakes = _Fakes(_failure(FetchFailureReason.not_found), _OZON_DATA)
    h = _Harness(
        monkeypatch,
        fakes,
        active_count=_LIMIT,
        tracked={counted: _product(counted)},
        alerts=[_active_alert(10)],
    )

    result = await h.service.preview_article(_TG, _ARTICLE)

    assert result == ArticlePreview(_OZON, _OZON_DATA)


@pytest.mark.asyncio
async def test_exhausted_limit_with_only_an_inactive_alert_still_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(_WB_DATA, _OZON_DATA)
    inactive = _active_alert(10)
    inactive.is_active = False
    h = _Harness(
        monkeypatch,
        fakes,
        active_count=_LIMIT,
        tracked={_OZON: _product(_OZON)},
        alerts=[inactive],
    )

    with pytest.raises(ProductLimitExceededError):
        await h.service.preview_article(_TG, _ARTICLE)

    assert fakes.calls == []


@pytest.mark.asyncio
async def test_one_below_the_limit_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(_WB_DATA, _OZON_DATA)
    h = _Harness(monkeypatch, fakes, active_count=_LIMIT - 1)

    result = await h.service.preview_article(_TG, _ARTICLE)

    assert result == ArticlePreview(_WB, _WB_DATA)


@pytest.mark.asyncio
async def test_limit_is_checked_for_the_internal_user_id_not_the_telegram_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(_WB_DATA, _OZON_DATA)
    h = _Harness(monkeypatch, fakes)

    await h.service.preview_article(_TG, _ARTICLE)

    h.users.get_or_create_by_tg_id.assert_awaited_once_with(_TG)
    h.products.count_active_by_user.assert_awaited_once_with(_USER_ID)


# --- preview_article: unsupported marketplace --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", [_WB, _OZON])
async def test_unsupported_marketplace_is_translated_to_the_domain_error(
    monkeypatch: pytest.MonkeyPatch, unsupported: Marketplace
) -> None:
    fakes = _Fakes(_failure(FetchFailureReason.not_found), _OZON_DATA)
    fakes.unsupported = unsupported
    h = _Harness(monkeypatch, fakes)

    with pytest.raises(MarketplaceNotSupportedError) as caught:
        await h.service.preview_article(_TG, _ARTICLE)

    assert str(unsupported) in str(caught.value)
    assert fakes.count(unsupported) == 0


# --- preview_article: journal ------------------------------------------------


@pytest.mark.asyncio
async def test_routing_wb_not_found_to_ozon_is_one_info_record_not_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(_failure(FetchFailureReason.not_found), _OZON_DATA)
    h = _Harness(monkeypatch, fakes)

    with _records() as records:
        await h.service.preview_article(_TG, _ARTICLE)

    assert len(records) == 1
    record = records[0]
    assert record["level"].name == "INFO"
    assert record["extra"]["marketplace"] == "wb"
    assert record["extra"]["article"] == _ARTICLE
    assert record["extra"]["reason"] == "not_found"
    assert "SECRET-DETAIL-MARKER" not in record["message"] + str(record["extra"])


@pytest.mark.asyncio
async def test_data_on_wb_logs_nothing_while_the_sink_does_catch_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ok = _Harness(monkeypatch, _Fakes(_WB_DATA, _OZON_DATA))
    broken = _Harness(
        monkeypatch, _Fakes(_failure(FetchFailureReason.blocked), _OZON_DATA)
    )

    with _records() as quiet:
        await ok.service.preview_article(_TG, _ARTICLE)
    with _records() as control:
        await broken.service.preview_article(_TG, _ARTICLE)

    assert quiet == []
    assert [r["level"].name for r in control] == ["WARNING"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "level"),
    [
        (FetchFailureReason.bad_payload, "ERROR"),
        (FetchFailureReason.out_of_stock, "INFO"),
        (FetchFailureReason.blocked, "WARNING"),
        (FetchFailureReason.transport_error, "WARNING"),
        (FetchFailureReason.not_found, "WARNING"),
    ],
)
async def test_ozon_failure_after_wb_not_found_is_logged_at_its_level_with_ozon(
    monkeypatch: pytest.MonkeyPatch, reason: FetchFailureReason, level: str
) -> None:
    fakes = _Fakes(_failure(FetchFailureReason.not_found), _failure(reason))
    h = _Harness(monkeypatch, fakes)

    with _records() as records:
        await h.service.preview_article(_TG, _ARTICLE)

    ozon_records = [r for r in records if r["extra"].get("marketplace") == "ozon"]
    assert len(ozon_records) == 1
    assert ozon_records[0]["level"].name == level
    assert ozon_records[0]["extra"]["reason"] == reason.value
    assert ozon_records[0]["extra"]["article"] == _ARTICLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "level"),
    [
        (FetchFailureReason.bad_payload, "ERROR"),
        (FetchFailureReason.out_of_stock, "INFO"),
        (FetchFailureReason.blocked, "WARNING"),
        (FetchFailureReason.transport_error, "WARNING"),
    ],
)
async def test_wb_failure_other_than_not_found_is_logged_once_at_its_level(
    monkeypatch: pytest.MonkeyPatch, reason: FetchFailureReason, level: str
) -> None:
    fakes = _Fakes(_failure(reason), _OZON_DATA)
    h = _Harness(monkeypatch, fakes)

    with _records() as records:
        await h.service.preview_article(_TG, _ARTICLE)

    assert len(records) == 1
    assert records[0]["level"].name == level
    assert records[0]["extra"]["marketplace"] == "wb"
    assert records[0]["extra"]["reason"] == reason.value
    assert "SECRET-DETAIL-MARKER" not in records[0]["message"] + str(
        records[0]["extra"]
    )


# --- find_tracked_article ----------------------------------------------------


@pytest.mark.asyncio
async def test_find_checks_wb_first_and_the_tracked_wb_product_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wb, ozon = _product(_WB, 10), _product(_OZON, 11)
    h = _Harness(
        monkeypatch, _Fakes(_WB_DATA, _OZON_DATA), tracked={_WB: wb, _OZON: ozon}
    )

    card = await h.service.find_tracked_article(_TG, _ARTICLE)

    assert isinstance(card, ProductCard)
    assert card.product is wb
    assert h.products.get_by_article_and_user.await_count == 1


@pytest.mark.asyncio
async def test_find_falls_back_to_ozon_when_wb_is_not_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ozon = _product(_OZON, 11)
    h = _Harness(monkeypatch, _Fakes(_WB_DATA, _OZON_DATA), tracked={_OZON: ozon})

    card = await h.service.find_tracked_article(_TG, _ARTICLE)

    assert card is not None
    assert card.product is ozon
    assert [c.args[2] for c in h.products.get_by_article_and_user.await_args_list] == [
        _WB,
        _OZON,
    ]


@pytest.mark.asyncio
async def test_find_returns_none_and_never_builds_a_client_when_nothing_is_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(_WB_DATA, _OZON_DATA)
    h = _Harness(monkeypatch, fakes)

    assert await h.service.find_tracked_article(_TG, _ARTICLE) is None
    assert fakes.calls == []
    h.alerts.get_by_user_and_product.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_scopes_the_lookup_to_the_internal_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, _Fakes(_WB_DATA, _OZON_DATA))

    await h.service.find_tracked_article(_TG, _ARTICLE)

    h.users.get_or_create_by_tg_id.assert_awaited_once_with(_TG)
    assert [c.args[1] for c in h.products.get_by_article_and_user.await_args_list] == [
        _USER_ID,
        _USER_ID,
    ]
