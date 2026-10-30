"""`TrackingService.resolve_short_link` and `preview` after the `_fetch` merge.

PAB-114, PROJECT.md 2.15 R22 items 3-5. Mocked boundaries: the repository layer
(it must stay untouched: zero calls) and the marketplace client factory. The
expected log levels are the policy table written out by hand, not read back
from `failure_log_level`.
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
from app.domain.links import ProductRef
from app.models.enums import Marketplace
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
from app.services.tracking import ShortLinkRefusal
from app.services.tracking import TrackingService

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_CODE = "7GeNMx2"
_LOCATION = "https://www.ozon.ru/product/some-slug-2622563074/?at=TOKEN&short=7GeNMx2"
_DATA = MarketplaceProductData(name="Ozon item", price=24_100)


class _Harness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        outcome: str | MarketplaceFetchFailure,
        *,
        unsupported: bool = False,
        active_count: int = 0,
    ) -> None:
        self.resolve_calls: list[str] = []
        self.product_calls: list[int] = []
        self.factory_calls: list[Marketplace] = []
        harness = self

        class _Client(BaseMarketplaceClient):
            async def get_product_data(self, article: int) -> MarketplaceFetchResult:
                harness.product_calls.append(article)
                return _DATA

            async def resolve_short_link(
                self, code: str
            ) -> str | MarketplaceFetchFailure:
                harness.resolve_calls.append(code)
                return outcome

        def factory(marketplace: Marketplace, _http: httpx.AsyncClient) -> _Client:
            self.factory_calls.append(marketplace)
            if unsupported:
                raise UnsupportedMarketplaceError(str(marketplace))
            return _Client.__new__(_Client)

        self.users = MagicMock(spec=UserRepository)
        self.users.get_or_create_by_tg_id.return_value = (
            User(id=7, tg_user_id=1),
            False,
        )
        self.users.get_by_tg_id.return_value = (
            self.users.get_or_create_by_tg_id.return_value[0]
        )
        self.products = MagicMock(spec=ProductRepository)
        self.products.count_active_by_user.return_value = active_count
        self.products.get_by_article_and_user.return_value = None
        self.alerts = MagicMock(spec=AlertRepository)
        monkeypatch.setattr(tracking_module, "UserRepository", lambda _s: self.users)
        monkeypatch.setattr(
            tracking_module, "ProductRepository", lambda _s: self.products
        )
        monkeypatch.setattr(tracking_module, "AlertRepository", lambda _s: self.alerts)
        self.service = TrackingService(
            session=cast(AsyncSession, MagicMock(spec=AsyncSession)),
            http_client=httpx.AsyncClient(),
            client_factory=factory,
            max_products_per_user=3,
        )

    def repository_calls(self) -> int:
        return (
            len(self.users.mock_calls)
            + len(self.products.mock_calls)
            + len(self.alerts.mock_calls)
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


@pytest.mark.asyncio
async def test_valid_location_gives_an_ozon_ref_and_touches_no_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, _LOCATION)

    result = await h.service.resolve_short_link(_CODE)

    assert result == ProductRef(Marketplace.ozon, 2622563074)
    assert h.resolve_calls == [_CODE]
    assert h.factory_calls == [Marketplace.ozon]
    assert h.product_calls == []
    assert h.repository_calls() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "host"),
    [
        (
            "https://www.ozon.ru/category/telefony-15501/?code=Cd9&q=secret",
            "www.ozon.ru",
        ),
        ("http://www.ozon.ru/product/x-2622563074/", "www.ozon.ru"),
        ("https://evil.example.com/product/x-2622563074/?q=secret", "evil.example.com"),
    ],
)
async def test_invalid_location_is_not_a_product_with_one_warning_naming_the_host(
    monkeypatch: pytest.MonkeyPatch, location: str, host: str
) -> None:
    h = _Harness(monkeypatch, location)

    with _records() as records:
        result = await h.service.resolve_short_link("Cd9")

    assert result is ShortLinkRefusal.not_a_product
    assert len(records) == 1
    record = records[0]
    assert record["level"].name == "WARNING"
    assert record["extra"] == {"location_host": host}
    dump = record["message"] + str(record["extra"])
    for leak in ("Cd9", "secret", "category", "telefony", "/product/", "2622563074"):
        assert leak not in dump
    assert h.repository_calls() == 0


@pytest.mark.asyncio
async def test_location_without_a_host_logs_an_empty_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, "https:///product/x-1/")

    with _records() as records:
        result = await h.service.resolve_short_link(_CODE)

    assert result is ShortLinkRefusal.not_a_product
    assert [r["extra"]["location_host"] for r in records] == [""]


@pytest.mark.asyncio
async def test_unparsable_location_logs_an_empty_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, "https://[")

    with _records() as records:
        result = await h.service.resolve_short_link(_CODE)

    assert result is ShortLinkRefusal.not_a_product
    assert [r["extra"]["location_host"] for r in records] == [""]


@pytest.mark.asyncio
async def test_overlong_host_is_cut_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, "https://" + "h" * 400 + ".com/product/x-1/")

    with _records() as records:
        await h.service.resolve_short_link(_CODE)

    assert len(records[0]["extra"]["location_host"]) == 255


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
async def test_client_failure_is_returned_as_is_and_logged_at_its_level(
    monkeypatch: pytest.MonkeyPatch, reason: FetchFailureReason, level: str
) -> None:
    failure = MarketplaceFetchFailure(reason=reason, detail="HTTP 0")
    h = _Harness(monkeypatch, failure)

    with _records() as records:
        result = await h.service.resolve_short_link("SecretCode9")

    assert result == failure
    assert len(records) == 1
    record = records[0]
    assert record["level"].name == level
    assert record["extra"] == {"reason": str(reason), "marketplace": "ozon"}
    assert "SecretCode9" not in record["message"]
    assert h.repository_calls() == 0


@pytest.mark.asyncio
async def test_unsupported_marketplace_becomes_a_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, _LOCATION, unsupported=True)

    with pytest.raises(MarketplaceNotSupportedError):
        await h.service.resolve_short_link(_CODE)

    assert h.resolve_calls == []


@pytest.mark.asyncio
async def test_preview_goes_through_the_client_and_returns_its_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, _LOCATION)

    result = await h.service.preview(1, Marketplace.ozon, 2622563074)

    assert result == _DATA
    assert h.product_calls == [2622563074]


@pytest.mark.asyncio
async def test_preview_over_the_limit_never_calls_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, _LOCATION, active_count=3)

    with pytest.raises(ProductLimitExceededError):
        await h.service.preview(1, Marketplace.ozon, 2622563074)

    assert h.product_calls == []


@pytest.mark.asyncio
async def test_preview_with_unsupported_marketplace_is_a_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, _LOCATION, unsupported=True)

    with pytest.raises(MarketplaceNotSupportedError):
        await h.service.preview(1, Marketplace.ozon, 2622563074)
