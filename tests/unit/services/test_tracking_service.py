"""Unit tests for app.services.tracking.TrackingService (PAB-068 AC4).

The boundary mocked here is the repository layer, the marketplace client
factory and the session's ``begin_nested``; nothing touches a database or
the network. The SQL itself and the real SAVEPOINT behaviour are covered by
``tests/integration/services/test_tracking_service_live.py``.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
import pytest
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.tracking as tracking_module
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.exceptions import TargetEqualsCurrentPriceError
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
from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient
from app.services.client_factory import get_client
from app.services.tracking import TrackingService

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_TG_USER_ID = 555
_USER_ID = 77  # deliberately different from the Telegram id
_ARTICLE = 860043555
_LIMIT = 3
_WB = Marketplace.wb


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception("orig"))


def _alert(
    *,
    target_price: int = 9_000,
    direction: AlertDirection = AlertDirection.below,
    is_active: bool = True,
    alert_id: int = 1,
) -> Alert:
    return Alert(
        id=alert_id,
        user_id=_USER_ID,
        product_id=10,
        target_price=target_price,
        direction=direction,
        is_active=is_active,
    )


def _product(*, product_id: int = 10) -> Product:
    return Product(
        id=product_id,
        user_id=_USER_ID,
        marketplace=_WB,
        article=_ARTICLE,
        product_name="Stored name",
        current_price=12_345,
    )


class _Savepoint:
    """Async context manager standing in for ``AsyncSession.begin_nested()``."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> _Savepoint:
        self._events.append("savepoint_begin")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self._events.append(
            "savepoint_rollback" if exc_type is not None else "savepoint_release"
        )
        return False


class _Harness(NamedTuple):
    service: TrackingService
    session: MagicMock
    users: MagicMock
    products: MagicMock
    alerts: MagicMock
    client: MagicMock
    factory: MagicMock
    http_client: httpx.AsyncClient
    events: list[str]


def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active_count: int = 0,
    existing_product: Product | None = None,
    alerts: list[Alert] | None = None,
    fetch_result: Any = None,
) -> _Harness:
    events: list[str] = []
    session = MagicMock(spec=AsyncSession)
    session.begin_nested = MagicMock(side_effect=lambda: _Savepoint(events))

    users = MagicMock(spec=UserRepository)
    users.get_or_create_by_tg_id.return_value = (
        User(id=_USER_ID, tg_user_id=_TG_USER_ID),
        True,
    )

    products = MagicMock(spec=ProductRepository)
    products.count_active_by_user.return_value = active_count
    products.get_by_article_and_user.return_value = existing_product

    async def create_product(data: dict[str, Any]) -> Product:
        events.append("create_product")
        return _product()

    products.create.side_effect = create_product

    alerts_repo = MagicMock(spec=AlertRepository)
    alerts_repo.get_by_user_and_product.return_value = alerts or []

    async def create_alert(data: dict[str, Any]) -> Alert:
        events.append("create_alert")
        return _alert(target_price=data["target_price"], direction=data["direction"])

    alerts_repo.create.side_effect = create_alert

    client = MagicMock(spec=BaseMarketplaceClient)
    client.get_product_data.return_value = (
        fetch_result
        if fetch_result is not None
        else MarketplaceProductData(name="Item", price=15_000)
    )
    factory = MagicMock(return_value=client)

    monkeypatch.setattr(tracking_module, "UserRepository", lambda _s: users)
    monkeypatch.setattr(tracking_module, "ProductRepository", lambda _s: products)
    monkeypatch.setattr(tracking_module, "AlertRepository", lambda _s: alerts_repo)

    http_client = httpx.AsyncClient()
    service = TrackingService(
        session=cast(AsyncSession, session),
        http_client=http_client,
        client_factory=factory,
        max_products_per_user=_LIMIT,
    )
    return _Harness(
        service, session, users, products, alerts_repo, client, factory,
        http_client, events,
    )  # fmt: skip


def _called_names(repo: MagicMock) -> set[str]:
    return {name for name, _args, _kwargs in repo.mock_calls}


async def _add(
    harness: _Harness, *, target: int = 9_000, product_name: str = "Fresh name"
) -> Alert:
    return await harness.service.add_tracking(
        _TG_USER_ID, _WB, _ARTICLE, product_name, 15_000, target
    )


# --- preview ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_returns_client_result_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = MarketplaceProductData(name="Item", price=15_000)
    harness = _build(monkeypatch, fetch_result=data)

    result = await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)

    assert result is data
    harness.factory.assert_called_once_with(_WB, harness.http_client)
    harness.client.get_product_data.assert_awaited_once_with(_ARTICLE)


@pytest.mark.asyncio
async def test_preview_registers_user_by_telegram_id_and_scopes_by_internal_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch, active_count=_LIMIT)

    with pytest.raises(ProductLimitExceededError):
        await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)

    harness.users.get_or_create_by_tg_id.assert_awaited_once_with(_TG_USER_ID)
    harness.products.count_active_by_user.assert_awaited_once_with(_USER_ID)
    harness.products.get_by_article_and_user.assert_awaited_once_with(
        _ARTICLE, _USER_ID, _WB
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("active_count", [_LIMIT, _LIMIT + 1])
async def test_preview_at_limit_raises_without_http_or_inserts(
    monkeypatch: pytest.MonkeyPatch, active_count: int
) -> None:
    harness = _build(monkeypatch, active_count=active_count)

    with pytest.raises(ProductLimitExceededError) as excinfo:
        await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)

    assert excinfo.value.limit == _LIMIT
    harness.factory.assert_not_called()
    harness.client.get_product_data.assert_not_called()
    harness.products.create.assert_not_called()
    harness.alerts.create.assert_not_called()


@pytest.mark.asyncio
async def test_preview_one_below_limit_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch, active_count=_LIMIT - 1)

    result = await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)

    assert isinstance(result, MarketplaceProductData)


@pytest.mark.asyncio
async def test_preview_at_limit_allows_product_already_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(
        monkeypatch,
        active_count=_LIMIT,
        existing_product=_product(),
        alerts=[_alert(is_active=False, alert_id=1), _alert(alert_id=2)],
    )

    result = await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)

    assert isinstance(result, MarketplaceProductData)
    harness.alerts.get_by_user_and_product.assert_awaited_once_with(_USER_ID, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alerts",
    [[], [_alert(is_active=False)]],
    ids=["no-alerts", "only-inactive-alerts"],
)
async def test_preview_at_limit_rejects_product_without_active_alert(
    monkeypatch: pytest.MonkeyPatch, alerts: list[Alert]
) -> None:
    harness = _build(
        monkeypatch, active_count=_LIMIT, existing_product=_product(), alerts=alerts
    )

    with pytest.raises(ProductLimitExceededError):
        await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)

    harness.factory.assert_not_called()


@pytest.mark.asyncio
async def test_preview_unsupported_marketplace_is_translated_and_sends_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    harness = _build(monkeypatch)
    real_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = TrackingService(
        session=cast(AsyncSession, harness.session),
        http_client=real_http,
        client_factory=get_client,
        max_products_per_user=_LIMIT,
    )

    with pytest.raises(MarketplaceNotSupportedError) as excinfo:
        await service.preview(_TG_USER_ID, Marketplace.ozon, 1234567)

    assert excinfo.value.marketplace == "ozon"
    assert requests == []
    await real_http.aclose()


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
async def test_preview_failure_is_returned_and_logged_once_at_expected_level(
    monkeypatch: pytest.MonkeyPatch, reason: FetchFailureReason, level: str
) -> None:
    failure = MarketplaceFetchFailure(reason=reason, detail="SECRET-DETAIL-MARKER")
    harness = _build(monkeypatch, fetch_result=failure)
    records: list[Record] = []

    def sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(sink, level="DEBUG")
    try:
        result = await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)
    finally:
        logger.remove(sink_id)

    assert result is failure
    assert len(records) == 1
    record = records[0]
    assert record["level"].name == level
    assert record["extra"]["reason"] == reason.value
    assert record["extra"]["marketplace"] == "wb"
    assert record["extra"]["article"] == _ARTICLE
    assert "SECRET-DETAIL-MARKER" not in record["message"]
    assert "SECRET-DETAIL-MARKER" not in str(record["extra"])


@pytest.mark.asyncio
async def test_preview_success_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _build(monkeypatch)
    records: list[Record] = []

    def sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(sink, level="DEBUG")
    try:
        await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)
    finally:
        logger.remove(sink_id)

    assert records == []


@pytest.mark.asyncio
async def test_preview_unknown_failure_reason_is_not_silently_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason match is exhaustive: a value outside the enum is a bug."""
    failure = MarketplaceFetchFailure.model_construct(
        reason=cast(Any, "brand_new_reason")
    )
    harness = _build(monkeypatch, fetch_result=failure)

    with pytest.raises(AssertionError):
        await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)


# --- add_tracking: product -------------------------------------------------


@pytest.mark.asyncio
async def test_add_tracking_creates_new_product_then_below_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch)

    alert = await _add(harness, target=9_000)

    harness.products.create.assert_awaited_once_with(
        {
            "user_id": _USER_ID,
            "marketplace": _WB,
            "article": _ARTICLE,
            "product_name": "Fresh name",
            "current_price": 15_000,
        }
    )
    harness.alerts.create.assert_awaited_once_with(
        {
            "user_id": _USER_ID,
            "product_id": 10,
            "target_price": 9_000,
            "direction": AlertDirection.below,
        }
    )
    assert alert.target_price == 9_000
    assert harness.events == [
        "savepoint_begin",
        "create_product",
        "savepoint_release",
        "savepoint_begin",
        "create_alert",
        "savepoint_release",
    ]


@pytest.mark.asyncio
async def test_add_tracking_reuses_existing_product_without_overwriting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _product(product_id=42)
    harness = _build(monkeypatch, existing_product=existing)

    await _add(harness, product_name="Different name")

    harness.products.create.assert_not_called()
    harness.products.patch.assert_not_called()
    assert existing.product_name == "Stored name"
    assert existing.current_price == 12_345
    harness.alerts.create.assert_awaited_once()
    assert harness.alerts.create.await_args is not None
    assert harness.alerts.create.await_args.args[0]["product_id"] == 42


@pytest.mark.asyncio
async def test_add_tracking_registers_user_and_scopes_every_call_by_internal_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch)

    await _add(harness)

    harness.users.get_or_create_by_tg_id.assert_awaited_once_with(_TG_USER_ID)
    harness.products.count_active_by_user.assert_awaited_once_with(_USER_ID)
    harness.products.get_by_article_and_user.assert_awaited_once_with(
        _ARTICLE, _USER_ID, _WB
    )
    harness.alerts.get_by_user_and_product.assert_awaited_once_with(_USER_ID, 10)
    assert _called_names(harness.products) <= {
        "count_active_by_user",
        "get_by_article_and_user",
        "create",
    }
    assert _called_names(harness.alerts) <= {"get_by_user_and_product", "create"}


@pytest.mark.asyncio
async def test_add_tracking_never_commits_or_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch)

    await _add(harness)

    cast(AsyncMock, harness.session.commit).assert_not_called()
    cast(AsyncMock, harness.session.rollback).assert_not_called()


@pytest.mark.asyncio
async def test_add_tracking_reuses_product_after_concurrent_insert_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = _product(product_id=99)
    harness = _build(monkeypatch)
    harness.products.get_by_article_and_user.side_effect = [None, winner]
    harness.products.create.side_effect = _integrity_error()

    await _add(harness)

    assert harness.products.get_by_article_and_user.await_count == 2
    assert harness.alerts.create.await_args is not None
    assert harness.alerts.create.await_args.args[0]["product_id"] == 99


@pytest.mark.asyncio
async def test_add_tracking_reraises_the_same_product_integrity_error_without_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = _integrity_error()
    harness = _build(monkeypatch)
    harness.products.create.side_effect = boom

    with pytest.raises(IntegrityError) as excinfo:
        await _add(harness)

    assert excinfo.value is boom
    harness.alerts.create.assert_not_called()


# --- add_tracking: limit ---------------------------------------------------


@pytest.mark.asyncio
async def test_add_tracking_at_limit_raises_without_inserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch, active_count=_LIMIT)

    with pytest.raises(ProductLimitExceededError) as excinfo:
        await _add(harness)

    assert excinfo.value.limit == _LIMIT
    harness.products.create.assert_not_called()
    harness.alerts.create.assert_not_called()


@pytest.mark.asyncio
async def test_add_tracking_at_limit_allows_another_alert_on_counted_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(
        monkeypatch,
        active_count=_LIMIT,
        existing_product=_product(),
        alerts=[_alert(target_price=8_000)],
    )

    alert = await _add(harness, target=9_000)

    assert alert.target_price == 9_000
    harness.products.create.assert_not_called()


# --- add_tracking: duplicates ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("is_active", [True, False])
async def test_add_tracking_detects_duplicate_before_insert(
    monkeypatch: pytest.MonkeyPatch, is_active: bool
) -> None:
    harness = _build(
        monkeypatch,
        existing_product=_product(),
        alerts=[_alert(target_price=9_000, is_active=is_active)],
    )

    with pytest.raises(DuplicateAlertError):
        await _add(harness, target=9_000)

    harness.alerts.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "other",
    [
        _alert(target_price=9_001),
        _alert(target_price=8_999),
        _alert(target_price=9_000, direction=AlertDirection.above),
    ],
    ids=["target-plus-one", "target-minus-one", "other-direction"],
)
async def test_add_tracking_does_not_treat_a_different_alert_as_duplicate(
    monkeypatch: pytest.MonkeyPatch, other: Alert
) -> None:
    harness = _build(monkeypatch, existing_product=_product(), alerts=[other])

    await _add(harness, target=9_000)

    harness.alerts.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_tracking_translates_alert_race_into_duplicate_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = _integrity_error()
    harness = _build(monkeypatch, existing_product=_product())
    harness.alerts.get_by_user_and_product.side_effect = [[], [_alert()]]
    harness.alerts.create.side_effect = boom

    with pytest.raises(DuplicateAlertError) as excinfo:
        await _add(harness, target=9_000)

    assert excinfo.value.__cause__ is boom
    assert harness.events == ["savepoint_begin", "savepoint_rollback"]


@pytest.mark.asyncio
async def test_add_tracking_reraises_the_same_alert_integrity_error_without_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = _integrity_error()
    harness = _build(monkeypatch, existing_product=_product())
    harness.alerts.create.side_effect = boom

    with pytest.raises(IntegrityError) as excinfo:
        await _add(harness)

    assert excinfo.value is boom
    assert harness.events == ["savepoint_begin", "savepoint_rollback"]


# --- add_tracking: direction (PAB-072) ---------------------------------------


@pytest.mark.asyncio
async def test_add_tracking_creates_an_above_alert_for_a_threshold_over_the_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch)

    alert = await _add(harness, target=20_000)  # current price is 15_000

    harness.alerts.create.assert_awaited_once_with(
        {
            "user_id": _USER_ID,
            "product_id": 10,
            "target_price": 20_000,
            "direction": AlertDirection.above,
        }
    )
    assert alert.direction is AlertDirection.above


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected"),
    [(14_999, AlertDirection.below), (15_001, AlertDirection.above)],
)
async def test_add_tracking_direction_flips_exactly_at_the_current_price(
    monkeypatch: pytest.MonkeyPatch, target: int, expected: AlertDirection
) -> None:
    harness = _build(monkeypatch)

    await _add(harness, target=target)

    created = harness.alerts.create.await_args.args[0]
    assert created["direction"] is expected


@pytest.mark.asyncio
async def test_add_tracking_equal_threshold_touches_no_repository_and_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch, existing_product=_product())

    with pytest.raises(TargetEqualsCurrentPriceError):
        await _add(harness, target=15_000)

    assert harness.users.mock_calls == []
    assert harness.products.mock_calls == []
    assert harness.alerts.mock_calls == []
    harness.factory.assert_not_called()
    harness.client.get_product_data.assert_not_called()
    assert harness.events == []


@pytest.mark.asyncio
async def test_add_tracking_below_alert_does_not_make_an_above_alert_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(
        monkeypatch,
        existing_product=_product(),
        alerts=[_alert(target_price=20_000, direction=AlertDirection.below)],
    )

    await _add(harness, target=20_000)

    harness.alerts.create.assert_awaited_once()
    assert harness.alerts.create.await_args.args[0]["direction"] is AlertDirection.above


@pytest.mark.asyncio
async def test_add_tracking_same_direction_and_target_is_a_duplicate_for_above(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(
        monkeypatch,
        existing_product=_product(),
        alerts=[_alert(target_price=20_000, direction=AlertDirection.above)],
    )

    with pytest.raises(DuplicateAlertError):
        await _add(harness, target=20_000)

    harness.alerts.create.assert_not_called()


@pytest.mark.asyncio
async def test_add_tracking_race_with_the_other_direction_is_not_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = _integrity_error()
    harness = _build(monkeypatch, existing_product=_product())
    harness.alerts.get_by_user_and_product.side_effect = [
        [],
        [_alert(target_price=20_000, direction=AlertDirection.below)],
    ]
    harness.alerts.create.side_effect = boom

    with pytest.raises(IntegrityError) as excinfo:
        await _add(harness, target=20_000)

    assert excinfo.value is boom


@pytest.mark.asyncio
async def test_add_tracking_race_with_the_same_direction_is_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _build(monkeypatch, existing_product=_product())
    harness.alerts.get_by_user_and_product.side_effect = [
        [],
        [_alert(target_price=20_000, direction=AlertDirection.above)],
    ]
    harness.alerts.create.side_effect = _integrity_error()

    with pytest.raises(DuplicateAlertError):
        await _add(harness, target=20_000)


# --- constant number of database round trips ---------------------------------


def _alerts_with_one_active(extra: int) -> list[Alert]:
    """One active alert (target 1) plus ``extra`` inactive ones."""
    inactive = [
        _alert(target_price=1_000 + i, alert_id=i + 2, is_active=False)
        for i in range(extra)
    ]
    return [_alert(target_price=1, alert_id=1), *inactive]


def _total_repo_calls(harness: _Harness) -> int:
    return sum(
        len(repo.mock_calls)
        for repo in (harness.users, harness.products, harness.alerts)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_alerts", [0, 1, 40])
async def test_add_tracking_round_trips_do_not_grow_with_existing_alerts(
    monkeypatch: pytest.MonkeyPatch, extra_alerts: int
) -> None:
    harness = _build(
        monkeypatch,
        active_count=_LIMIT,
        existing_product=_product(),
        alerts=_alerts_with_one_active(extra_alerts),
    )

    await _add(harness, target=9_000)

    # user 1, count 1, product + alerts for the limit 2, product 1,
    # duplicate check 1, create 1: fixed whatever the alert count is.
    assert _total_repo_calls(harness) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_alerts", [0, 40])
async def test_preview_round_trips_do_not_grow_with_existing_alerts(
    monkeypatch: pytest.MonkeyPatch, extra_alerts: int
) -> None:
    harness = _build(
        monkeypatch,
        active_count=_LIMIT,
        existing_product=_product(),
        alerts=_alerts_with_one_active(extra_alerts),
    )

    await harness.service.preview(_TG_USER_ID, _WB, _ARTICLE)

    assert _total_repo_calls(harness) == 4


def test_tracking_module_leaves_the_transaction_to_the_caller() -> None:
    source = tracking_module.__loader__.get_source(tracking_module.__name__)  # type: ignore[union-attr]
    assert source is not None
    for forbidden in (
        ".commit(",
        ".rollback(",
        ".get_by_id(",
        ".patch(",
        ".delete(",
        ".list_paginated(",
    ):
        assert forbidden not in source, forbidden
