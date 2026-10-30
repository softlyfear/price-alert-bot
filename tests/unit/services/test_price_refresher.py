"""Unit tests for ``PriceRefresher`` (PAB-100 AC1-AC4, AC6, AC7).

Replaced boundaries: ``PriceService`` (its three methods the refresher uses),
the two repositories and the sessionmaker. The ``PriceService`` double records
what it was asked and answers from a table keyed by article; the repositories
filter by owner the way the SQL does, so a refresher that passed the Telegram id
instead of the internal user id, or forgot the owner, would be seen. The writes
themselves (``apply_result`` against PostgreSQL) are covered by
``tests/integration/services/test_price_refresher_live.py``.

Nothing waits on the real clock for synchronisation: gates are
``asyncio.Event`` objects and ``_settle`` yields to the loop. The deadline tests
use a deadline of 0.2 s, as the ticket allows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from types import TracebackType
from typing import TYPE_CHECKING
from typing import Self
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.services.price_service import PriceRefresher
from app.services.price_service import PriceService
from app.services.price_service import RefreshFailure
from app.services.price_service import RefreshGone
from app.services.price_service import RefreshLimiter
from app.services.price_service import RefreshReport
from app.services.price_service import RefreshThrottled

if TYPE_CHECKING:
    from loguru import Record

pytestmark = pytest.mark.asyncio

_TG_USER = 555
_USER_ID = 77  # deliberately different from the Telegram id
_OTHER_USER_ID = 88
_WB = Marketplace.wb
_OZON = Marketplace.ozon
_WB_BATCH = 100
_SHORT_DEADLINE = 0.05
_HANG_GUARD = 3.0  # only stops a hung test, never synchronises one


def _ok(article: int) -> MarketplaceProductData:
    return MarketplaceProductData(name="Item", price=10_000 + article)


def _blocked() -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(reason=FetchFailureReason.blocked, detail="stub")


def _failure(reason: FetchFailureReason) -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(reason=reason, detail="stub")


def _product(
    product_id: int,
    marketplace: Marketplace = _WB,
    *,
    user_id: int = _USER_ID,
) -> Product:
    return Product(
        id=product_id,
        user_id=user_id,
        marketplace=marketplace,
        article=product_id * 10,  # the article is told apart from the id
        product_name=f"Item {product_id}",
        current_price=5_000,
    )


class _FakeTransaction:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> Self:
        self._session.tx_active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._session.tx_active = False
        self._session.outcome = "commit" if exc_type is None else "rollback"


class _FakeSession:
    """Stands in for ``AsyncSession``: tracks open state and the transaction end."""

    def __init__(self, owner: _FakeSessionmaker) -> None:
        self._owner = owner
        self.tx_active = False
        self.outcome: str | None = None

    async def __aenter__(self) -> Self:
        self._owner.open += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._owner.open -= 1
        if exc_type is not None:
            self.outcome = "rollback"
        self.tx_active = False

    async def commit(self) -> None:
        self.outcome = "commit"
        self.tx_active = False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)


class _FakeSessionmaker:
    def __init__(self) -> None:
        self.open = 0
        self.created: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession(self)
        self.created.append(session)
        return session

    def as_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return cast(async_sessionmaker[AsyncSession], self)


class _FakeUsers:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def get_by_tg_id(self, tg_user_id: int) -> User | None:
        self.calls.append(tg_user_id)
        return User(id=_USER_ID, tg_user_id=tg_user_id)


class _FakeProducts:
    """Owner-scoped lookups, like the SQL they stand for."""

    def __init__(self, products: Sequence[Product]) -> None:
        self._products = list(products)

    async def get_by_user_id(self, user_id: int) -> list[Product]:
        return [p for p in self._products if p.user_id == user_id]

    async def get_by_id_for_user(self, product_id: int, user_id: int) -> Product | None:
        return next(
            (p for p in self._products if p.id == product_id and p.user_id == user_id),
            None,
        )


class _FakePriceService:
    """The three ``PriceService`` methods the refresher uses, with a call log."""

    def __init__(self, sessions: _FakeSessionmaker) -> None:
        self._sessions = sessions
        self.batch_sizes: dict[Marketplace, int] = {_WB: _WB_BATCH, _OZON: 1}
        self.batch_size_error: dict[Marketplace, Exception] = {}
        self.results: dict[int, MarketplaceFetchResult] = {}
        self.fetch_error: Exception | None = None
        self.fetch_hook: Callable[[Marketplace, list[int]], Awaitable[None]] | None = (
            None
        )
        self.apply_hook: Callable[[int], Awaitable[None]] | None = None
        self.fetch_calls: list[tuple[Marketplace, list[int]]] = []
        self.open_sessions_at_fetch: list[int] = []
        self.applied: list[tuple[int, MarketplaceFetchResult]] = []
        self.tx_active_at_apply: list[bool] = []
        self.active = 0
        self.peak = 0

    def batch_size(self, marketplace: Marketplace) -> int:
        if marketplace in self.batch_size_error:
            raise self.batch_size_error[marketplace]
        return self.batch_sizes[marketplace]

    async def fetch_prices(
        self, marketplace: Marketplace, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        asked = list(articles)
        self.fetch_calls.append((marketplace, asked))
        self.open_sessions_at_fetch.append(self._sessions.open)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.fetch_hook is not None:
                await self.fetch_hook(marketplace, asked)
            if self.fetch_error is not None:
                raise self.fetch_error
            return {a: self.results.get(a, _ok(a)) for a in asked}
        finally:
            self.active -= 1

    async def apply_result(
        self, product_id: int, result: MarketplaceFetchResult, session: AsyncSession
    ) -> None:
        cast(
            _FakeSession, session
        ).tx_active = True  # real apply_result begins the write
        self.tx_active_at_apply.append(cast(_FakeSession, session).tx_active)
        if self.apply_hook is not None:
            await self.apply_hook(product_id)
        self.applied.append((product_id, result))

    def as_service(self) -> PriceService:
        return cast(PriceService, self)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Rig:
    def __init__(
        self,
        products: Sequence[Product],
        *,
        concurrency: int = 5,
        deadline: float = 20.0,
    ) -> None:
        self.sessions = _FakeSessionmaker()
        self.service = _FakePriceService(self.sessions)
        self.users = _FakeUsers()
        self.clock = _Clock()
        product_repo = _FakeProducts(products)
        self.refresher = PriceRefresher(
            self.service.as_service(),
            self.sessions.as_sessionmaker(),
            lambda _s: cast(ProductRepository, product_repo),
            lambda _s: cast(UserRepository, self.users),
            concurrency=concurrency,
            limiter=RefreshLimiter(60.0, self.clock),
            deadline_seconds=deadline,
        )


def _consistent(report: RefreshReport) -> RefreshReport:
    """AC7: every report adds up; also no zero counters in ``failures``."""
    assert report.updated + sum(report.failures.values()) == report.total
    assert all(count > 0 for count in report.failures.values())
    return report


async def _settle() -> None:
    """Let every ready task run to its next suspension point."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _hang_forever(*_args: object) -> None:
    await asyncio.Event().wait()


async def _wait_past_deadline(started_at: float) -> None:
    """Wait on the loop clock until the loop clock is past `_SHORT_DEADLINE` + margin.

    `started_at` is read before the refresh begins, so the refresher's own deadline
    (taken later) is at most a few microseconds beyond `started_at + deadline`.
    """
    reached = asyncio.Event()
    asyncio.get_running_loop().call_at(started_at + _SHORT_DEADLINE + 0.05, reached.set)
    await reached.wait()


# --- batches and the shape of the requests ---------------------------------------


async def test_wb_products_go_in_batches_of_a_hundred_and_ozon_one_by_one() -> None:
    wb = [_product(i, _WB) for i in range(1, 151)]
    ozon = [_product(i, _OZON) for i in range(1001, 1004)]
    rig = _Rig([*wb, *ozon])

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    wb_calls = [a for m, a in rig.service.fetch_calls if m is _WB]
    ozon_calls = [a for m, a in rig.service.fetch_calls if m is _OZON]
    assert sorted(len(a) for a in wb_calls) == [50, 100]
    assert sorted(len(a) for a in ozon_calls) == [1, 1, 1]
    assert sorted(a for call in wb_calls for a in call) == [
        i * 10 for i in range(1, 151)
    ]
    assert report == RefreshReport(total=153, updated=153, failures={})


async def test_every_product_gets_the_result_of_its_own_article() -> None:
    rig = _Rig([_product(1), _product(2), _product(3)])
    rig.service.results = {20: _failure(FetchFailureReason.out_of_stock)}

    await rig.refresher.refresh_list(_TG_USER)

    applied = dict(rig.service.applied)
    assert applied[1] == _ok(10)
    assert applied[2] == _failure(FetchFailureReason.out_of_stock)
    assert applied[3] == _ok(30)


async def test_batch_size_comes_from_the_price_service() -> None:
    rig = _Rig([_product(i) for i in range(1, 6)])
    rig.service.batch_sizes[_WB] = 2

    await rig.refresher.refresh_list(_TG_USER)

    assert sorted(len(a) for _m, a in rig.service.fetch_calls) == [1, 2, 2]


async def test_an_empty_list_makes_no_request_and_reports_zero() -> None:
    rig = _Rig([])

    report = await rig.refresher.refresh_list(_TG_USER)

    assert report == RefreshReport(total=0, updated=0, failures={})
    assert rig.service.fetch_calls == []
    assert rig.service.applied == []


async def test_refresh_list_touches_only_the_callers_products() -> None:
    rig = _Rig([_product(1), _product(2), _product(9, user_id=_OTHER_USER_ID)])

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report.total == 2
    assert sorted(pid for pid, _r in rig.service.applied) == [1, 2]
    assert [a for _m, call in rig.service.fetch_calls for a in call] == [10, 20]
    assert rig.users.calls == [_TG_USER]


# --- one product ----------------------------------------------------------------


async def test_refresh_product_fetches_and_applies_only_that_product() -> None:
    rig = _Rig([_product(1), _product(2)])

    result = await rig.refresher.refresh_product(_TG_USER, 2)

    assert result == RefreshReport(total=1, updated=1, failures={})
    assert rig.service.fetch_calls == [(_WB, [20])]
    assert [pid for pid, _r in rig.service.applied] == [2]


async def test_refresh_product_of_someone_else_is_gone_without_a_request() -> None:
    rig = _Rig([_product(1), _product(9, user_id=_OTHER_USER_ID)])

    result = await rig.refresher.refresh_product(_TG_USER, 9)

    assert result == RefreshGone()
    assert rig.service.fetch_calls == []
    assert rig.service.applied == []


async def test_refresh_product_of_a_missing_id_is_gone_without_a_request() -> None:
    rig = _Rig([_product(1)])

    result = await rig.refresher.refresh_product(_TG_USER, 123_456)

    assert result == RefreshGone()
    assert rig.service.fetch_calls == []


async def test_refresh_product_looks_the_user_up_by_the_telegram_id() -> None:
    rig = _Rig([_product(1)])

    await rig.refresher.refresh_product(_TG_USER, 1)

    assert rig.users.calls == [_TG_USER]


# --- sessions ---------------------------------------------------------------------


async def test_no_session_is_open_during_any_fetch() -> None:
    rig = _Rig([_product(1), _product(2, _OZON), _product(3, _OZON)])

    await rig.refresher.refresh_list(_TG_USER)

    assert len(rig.service.open_sessions_at_fetch) == 3
    assert rig.service.open_sessions_at_fetch == [0, 0, 0]
    assert rig.sessions.open == 0


async def test_no_session_is_open_during_a_single_product_fetch() -> None:
    rig = _Rig([_product(1)])

    await rig.refresher.refresh_product(_TG_USER, 1)

    assert rig.service.open_sessions_at_fetch == [0]


async def test_every_product_is_applied_in_its_own_transaction() -> None:
    rig = _Rig([_product(1), _product(2), _product(3)])

    await rig.refresher.refresh_list(_TG_USER)

    # one selection session plus one session per product; the selection and each
    # write are committed transactions of their own
    assert len(rig.sessions.created) == 4
    assert [s.outcome for s in rig.sessions.created] == ["commit"] * 4
    assert rig.service.tx_active_at_apply == [True, True, True]


# --- concurrency ------------------------------------------------------------------


async def test_in_flight_fetches_never_exceed_the_concurrency() -> None:
    rig = _Rig([_product(i, _OZON) for i in range(1, 7)], concurrency=2)
    release = asyncio.Event()
    two_started = asyncio.Event()

    async def hold(_m: Marketplace, _a: list[int]) -> None:
        if len(rig.service.fetch_calls) == 2:
            two_started.set()
        await release.wait()

    rig.service.fetch_hook = hold
    task = asyncio.create_task(rig.refresher.refresh_list(_TG_USER))
    async with asyncio.timeout(_HANG_GUARD):
        await two_started.wait()
    await _settle()

    assert len(rig.service.fetch_calls) == 2  # the third waits for the semaphore
    assert rig.service.peak == 2
    release.set()
    report = _consistent(await task)
    assert len(rig.service.fetch_calls) == 6
    assert rig.service.peak == 2
    assert report.updated == 6


async def test_concurrency_one_runs_the_batches_one_after_another() -> None:
    rig = _Rig([_product(i, _OZON) for i in range(1, 4)], concurrency=1)

    async def spin(_m: Marketplace, _a: list[int]) -> None:
        await _settle()

    rig.service.fetch_hook = spin

    _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert rig.service.peak == 1


# --- blocked series ---------------------------------------------------------------


async def test_after_the_first_block_the_rest_of_the_marketplace_is_not_requested() -> (
    None
):
    rig = _Rig(
        [_product(i, _OZON) for i in range(1, 4)] + [_product(10), _product(11)],
        concurrency=1,
    )
    rig.service.results = {10: _blocked()}  # the first Ozon article answers "blocked"

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    ozon_fetches = [a for m, a in rig.service.fetch_calls if m is _OZON]
    assert ozon_fetches == [[10]]
    assert report.failures == {RefreshFailure.blocked: 3}
    assert report.updated == 2  # both Wildberries products are refreshed


async def test_products_skipped_after_a_block_are_not_applied() -> None:
    rig = _Rig([_product(i, _OZON) for i in range(1, 4)], concurrency=1)
    rig.service.results = {10: _blocked()}

    await rig.refresher.refresh_list(_TG_USER)

    # `last_checked_at` of the skipped products stays: no write happens for them
    assert [pid for pid, _r in rig.service.applied] == [1]


async def test_the_wildberries_batches_still_run_after_an_ozon_block() -> None:
    rig = _Rig(
        [_product(1, _OZON), _product(2, _OZON), _product(3), _product(4)],
        concurrency=1,
    )
    rig.service.results = {10: _blocked()}
    rig.service.batch_sizes[_WB] = 1

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert [m for m, _a in rig.service.fetch_calls].count(_WB) == 2
    assert report.updated == 2
    assert report.failures == {RefreshFailure.blocked: 2}


async def test_a_block_inside_a_batch_stops_later_batches_but_keeps_its_rest() -> None:
    rig = _Rig([_product(i) for i in range(1, 5)], concurrency=1)
    rig.service.batch_sizes[_WB] = 2
    rig.service.results = {10: _blocked()}  # first batch: products 1 (blocked) and 2

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert rig.service.fetch_calls == [(_WB, [10, 20])]
    assert dict(rig.service.applied)[2] == _ok(20)
    assert report.updated == 1
    assert report.failures == {RefreshFailure.blocked: 3}


async def test_without_a_block_every_batch_of_the_marketplace_runs() -> None:
    rig = _Rig([_product(i, _OZON) for i in range(1, 4)], concurrency=1)

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert len(rig.service.fetch_calls) == 3
    assert report.updated == 3


async def test_the_block_does_not_leak_into_the_next_refresh() -> None:
    rig = _Rig([_product(i, _OZON) for i in range(1, 3)], concurrency=1)
    rig.service.results = {10: _blocked()}
    await rig.refresher.refresh_list(_TG_USER)
    rig.service.results = {}
    rig.service.fetch_calls.clear()

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert len(rig.service.fetch_calls) == 2
    assert report.updated == 2


# --- failure categories -----------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        (FetchFailureReason.blocked, RefreshFailure.blocked),
        (FetchFailureReason.transport_error, RefreshFailure.transport_error),
        (FetchFailureReason.out_of_stock, RefreshFailure.out_of_stock),
        (FetchFailureReason.not_found, RefreshFailure.not_found),
        (FetchFailureReason.bad_payload, RefreshFailure.bad_payload),
    ],
)
async def test_each_marketplace_failure_has_its_own_category(
    reason: FetchFailureReason, category: RefreshFailure
) -> None:
    rig = _Rig([_product(1), _product(2)])
    rig.service.results = {10: _failure(reason)}

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(total=2, updated=1, failures={category: 1})
    # a failed fetch is still written (it moves `last_checked_at` only)
    assert sorted(pid for pid, _r in rig.service.applied) == [1, 2]


async def test_failures_are_counted_per_category() -> None:
    rig = _Rig([_product(i) for i in range(1, 6)])
    rig.service.results = {
        10: _failure(FetchFailureReason.not_found),
        20: _failure(FetchFailureReason.not_found),
        30: _failure(FetchFailureReason.transport_error),
    }

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report.updated == 2
    assert report.failures == {
        RefreshFailure.not_found: 2,
        RefreshFailure.transport_error: 1,
    }


async def test_a_single_product_failure_is_reported_on_the_card_path() -> None:
    rig = _Rig([_product(1, _OZON)])
    rig.service.results = {10: _blocked()}

    result = await rig.refresher.refresh_product(_TG_USER, 1)

    assert result == RefreshReport(1, 0, {RefreshFailure.blocked: 1})


# --- exceptions -------------------------------------------------------------------


async def test_a_failing_write_is_one_error_and_the_others_are_applied(
    log_records: list[Record],
) -> None:
    rig = _Rig([_product(1), _product(2), _product(3)])

    async def explode_on_two(product_id: int) -> None:
        if product_id == 2:
            raise RuntimeError("boom")

    rig.service.apply_hook = explode_on_two

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(3, 2, {RefreshFailure.error: 1})
    assert sorted(pid for pid, _r in rig.service.applied) == [1, 3]
    errors = [r for r in log_records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["extra"]["product_id"] == 2
    assert errors[0]["exception"] is not None
    assert errors[0]["exception"].type is RuntimeError


async def test_a_failing_write_rolls_back_only_its_own_transaction() -> None:
    rig = _Rig([_product(1), _product(2), _product(3)])

    async def explode_on_two(product_id: int) -> None:
        if product_id == 2:
            raise RuntimeError("boom")

    rig.service.apply_hook = explode_on_two

    await rig.refresher.refresh_list(_TG_USER)

    outcomes = [s.outcome for s in rig.sessions.created]
    assert outcomes.count("rollback") == 1
    assert outcomes.count("commit") == 3  # selection plus two good writes


async def test_the_error_log_has_no_secret_url_or_article_data(
    log_records: list[Record],
) -> None:
    rig = _Rig([_product(1)])

    async def explode(_product_id: int) -> None:
        raise RuntimeError("boom")

    rig.service.apply_hook = explode

    await rig.refresher.refresh_list(_TG_USER)

    for record in log_records:
        assert "http" not in record["message"]
        assert set(record["extra"]) <= {
            "product_id",
            "tg_user_id",
            "scope",
            "total",
            "updated",
            "failures",
        }


async def test_a_fetch_exception_fails_only_the_products_of_that_batch(
    log_records: list[Record],
) -> None:
    rig = _Rig([_product(1, _OZON), _product(2), _product(3)])

    async def explode_for_ozon(marketplace: Marketplace, _a: list[int]) -> None:
        if marketplace is _OZON:
            raise RuntimeError("boom")

    rig.service.fetch_hook = explode_for_ozon

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(3, 2, {RefreshFailure.error: 1})
    assert sorted(pid for pid, _r in rig.service.applied) == [2, 3]
    assert [r["extra"].get("marketplace") for r in log_records if r["exception"]] == [
        "ozon"
    ]


async def test_a_fetch_exception_does_not_stop_later_batches_of_its_marketplace() -> (
    None
):
    rig = _Rig([_product(1, _OZON), _product(2, _OZON)], concurrency=1)
    failures = iter([RuntimeError("boom"), None])

    async def explode_once(_m: Marketplace, _a: list[int]) -> None:
        error = next(failures)
        if error is not None:
            raise error

    rig.service.fetch_hook = explode_once

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(2, 1, {RefreshFailure.error: 1})


async def test_a_batch_size_exception_fails_that_marketplace_and_keeps_the_other() -> (
    None
):
    rig = _Rig([_product(1, _OZON), _product(2, _OZON), _product(3)])
    rig.service.batch_size_error[_OZON] = RuntimeError("no client")

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(3, 1, {RefreshFailure.error: 2})
    assert [m for m, _a in rig.service.fetch_calls] == [_WB]


async def test_a_batch_missing_an_article_fails_only_that_product(
    log_records: list[Record],
) -> None:
    rig = _Rig([_product(1), _product(2)])
    real_fetch = rig.service.fetch_prices

    async def drop_article_ten(
        marketplace: Marketplace, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        results = await real_fetch(marketplace, articles)
        results.pop(10)
        return results

    setattr(rig.service, "fetch_prices", drop_article_ten)  # noqa: B010

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(2, 1, {RefreshFailure.error: 1})
    assert [pid for pid, _r in rig.service.applied] == [2]
    assert [r["extra"]["product_id"] for r in log_records if r["exception"]] == [1]


# --- deadline and cancellation ----------------------------------------------------


async def test_a_hanging_fetch_ends_at_the_deadline_with_the_rest_kept() -> None:
    rig = _Rig([_product(1), _product(2), _product(3, _OZON)], deadline=0.2)

    async def hang_for_ozon(marketplace: Marketplace, _a: list[int]) -> None:
        if marketplace is _OZON:
            await _hang_forever()

    rig.service.fetch_hook = hang_for_ozon

    async with asyncio.timeout(1.0):
        report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(3, 2, {RefreshFailure.timed_out: 1})
    assert sorted(pid for pid, _r in rig.service.applied) == [1, 2]


async def test_batches_still_waiting_for_the_semaphore_at_the_deadline_time_out() -> (
    None
):
    rig = _Rig([_product(i, _OZON) for i in range(1, 4)], concurrency=1, deadline=0.2)
    rig.service.fetch_hook = lambda *_a: _hang_forever()

    async with asyncio.timeout(1.0):
        report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert len(rig.service.fetch_calls) == 1
    assert report == RefreshReport(3, 0, {RefreshFailure.timed_out: 3})
    assert rig.service.applied == []


async def test_a_write_started_before_the_deadline_is_kept_and_counted_as_updated() -> (
    None
):
    rig = _Rig([_product(1)], deadline=_SHORT_DEADLINE)
    started_at = asyncio.get_running_loop().time()

    async def deadline_passes_inside_the_write(_product_id: int) -> None:
        await _wait_past_deadline(started_at)

    rig.service.apply_hook = deadline_passes_inside_the_write

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(1, 1, {})
    assert [pid for pid, _r in rig.service.applied] == [1]
    assert [s.outcome for s in rig.sessions.created] == ["commit", "commit"]
    assert rig.sessions.open == 0


async def test_no_write_starts_after_the_deadline_and_the_rest_is_timed_out() -> None:
    rig = _Rig([_product(1), _product(2), _product(3)], deadline=_SHORT_DEADLINE)
    started_at = asyncio.get_running_loop().time()

    async def deadline_passes_inside_the_first_write(product_id: int) -> None:
        if product_id == 1:
            await _wait_past_deadline(started_at)

    rig.service.apply_hook = deadline_passes_inside_the_first_write

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(3, 1, {RefreshFailure.timed_out: 2})
    assert [pid for pid, _r in rig.service.applied] == [1]
    assert len(rig.service.tx_active_at_apply) == 1  # apply_result never began for 2, 3
    assert len(rig.sessions.created) == 2  # selection plus the one write


async def test_a_deadline_inside_a_wait_for_the_semaphore_leaves_it_usable() -> None:
    rig = _Rig([_product(i, _OZON) for i in range(1, 4)], concurrency=1, deadline=0.1)
    rig.service.fetch_hook = lambda *_a: _hang_forever()
    first = await rig.refresher.refresh_list(_TG_USER)
    rig.service.fetch_hook = None
    rig.service.fetch_calls.clear()

    async with asyncio.timeout(_HANG_GUARD):
        second = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert first == RefreshReport(3, 0, {RefreshFailure.timed_out: 3})
    assert second == RefreshReport(3, 3, {})
    assert len(rig.service.fetch_calls) == 3


async def test_an_own_timeout_error_of_a_fetch_is_an_error_not_a_timed_out(
    log_records: list[Record],
) -> None:
    rig = _Rig([_product(1), _product(2)])  # the 20 s deadline is far away
    rig.service.fetch_error = TimeoutError("socket read timed out")

    report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(2, 0, {RefreshFailure.error: 2})
    assert rig.service.applied == []
    assert len([r for r in log_records if r["exception"]]) == 1


async def test_the_deadline_comes_from_the_constructor() -> None:
    rig = _Rig([_product(1, _OZON)], deadline=0.05)
    rig.service.fetch_hook = lambda *_a: _hang_forever()

    async with asyncio.timeout(1.0):
        report = await rig.refresher.refresh_list(_TG_USER)

    assert report.failures == {RefreshFailure.timed_out: 1}


async def test_an_external_cancel_propagates_instead_of_becoming_a_report() -> None:
    rig = _Rig([_product(1), _product(2, _OZON)])
    started = asyncio.Event()

    async def hang(_m: Marketplace, _a: list[int]) -> None:
        started.set()
        await _hang_forever()

    rig.service.fetch_hook = hang
    task = asyncio.create_task(rig.refresher.refresh_list(_TG_USER))
    async with asyncio.timeout(_HANG_GUARD):
        await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_an_external_cancel_stops_the_work_in_flight() -> None:
    rig = _Rig([_product(i, _OZON) for i in range(1, 4)], concurrency=1)
    started = asyncio.Event()

    async def hang(_m: Marketplace, _a: list[int]) -> None:
        started.set()
        await _hang_forever()

    rig.service.fetch_hook = hang
    task = asyncio.create_task(rig.refresher.refresh_list(_TG_USER))
    async with asyncio.timeout(_HANG_GUARD):
        await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert len(rig.service.fetch_calls) == 1  # the waiting batches never started
    assert rig.service.active == 0


async def test_an_external_cancel_releases_the_semaphore() -> None:
    rig = _Rig([_product(1, _OZON), _product(2, _OZON)], concurrency=1)
    started = asyncio.Event()

    async def hang(_m: Marketplace, _a: list[int]) -> None:
        started.set()
        await _hang_forever()

    rig.service.fetch_hook = hang
    task = asyncio.create_task(rig.refresher.refresh_list(_TG_USER))
    async with asyncio.timeout(_HANG_GUARD):
        await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rig.service.fetch_hook = None

    async with asyncio.timeout(_HANG_GUARD):
        report = _consistent(await rig.refresher.refresh_list(_TG_USER))

    assert report == RefreshReport(2, 2, {})


# --- summary log and budget -------------------------------------------------------


async def test_the_result_is_logged_once_at_info_with_the_counters(
    log_records: list[Record],
) -> None:
    rig = _Rig([_product(1), _product(2)])
    rig.service.results = {10: _failure(FetchFailureReason.not_found)}

    await rig.refresher.refresh_list(_TG_USER)

    summaries = [r for r in log_records if "total" in r["extra"]]
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "INFO"
    assert summaries[0]["extra"] == {
        "tg_user_id": _TG_USER,
        "scope": "list",
        "total": 2,
        "updated": 1,
        "failures": {"not_found": 1},
    }


async def test_an_empty_refresh_is_logged_too(log_records: list[Record]) -> None:
    rig = _Rig([])

    await rig.refresher.refresh_list(_TG_USER)

    summaries = [r for r in log_records if "total" in r["extra"]]
    assert [r["extra"] for r in summaries] == [
        {
            "tg_user_id": _TG_USER,
            "scope": "list",
            "total": 0,
            "updated": 0,
            "failures": {},
        }
    ]


async def test_the_card_refresh_summary_is_tagged_with_the_card_scope(
    log_records: list[Record],
) -> None:
    rig = _Rig([_product(1)])

    await rig.refresher.refresh_product(_TG_USER, 1)

    summaries = [r for r in log_records if "total" in r["extra"]]
    assert [(r["extra"]["tg_user_id"], r["extra"]["scope"]) for r in summaries] == [
        (_TG_USER, "card")
    ]


@pytest.mark.parametrize("scope", ["list", "card"])
async def test_the_deadline_warning_names_the_user_and_the_scope(
    scope: str, log_records: list[Record]
) -> None:
    rig = _Rig([_product(1, _OZON)], deadline=_SHORT_DEADLINE)
    rig.service.fetch_hook = lambda *_a: _hang_forever()

    if scope == "list":
        await rig.refresher.refresh_list(_TG_USER)
    else:
        await rig.refresher.refresh_product(_TG_USER, 1)

    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0]["message"] == "Manual refresh hit the deadline"
    assert warnings[0]["extra"] == {"tg_user_id": _TG_USER, "scope": scope}


async def test_reserve_is_one_budget_for_the_list_and_the_card() -> None:
    rig = _Rig([_product(1)])

    assert rig.refresher.reserve(_TG_USER) is None
    assert rig.refresher.reserve(_TG_USER) == RefreshThrottled(60)
    rig.clock.now += 60.0
    assert rig.refresher.reserve(_TG_USER) is None


async def test_reserve_of_one_user_does_not_throttle_another() -> None:
    rig = _Rig([])

    rig.refresher.reserve(_TG_USER)

    assert rig.refresher.reserve(_TG_USER + 1) is None


async def test_refresh_itself_does_not_spend_the_budget() -> None:
    rig = _Rig([_product(1)])

    await rig.refresher.refresh_list(_TG_USER)
    await rig.refresher.refresh_product(_TG_USER, 1)

    assert rig.refresher.reserve(_TG_USER) is None


@pytest.mark.asyncio
async def test_refresh_product_of_unknown_user_is_gone_without_creating_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig([])
    monkeypatch.setattr(rig.users, "get_by_tg_id", AsyncMock(return_value=None))
    assert isinstance(await rig.refresher.refresh_product(_TG_USER, 123), RefreshGone)
    assert rig.service.fetch_calls == []
