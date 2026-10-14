"""Tests for app.scheduler (PAB-069 AC6, AC7).

Boundaries replaced: the sessionmaker (a fake that records close/commit/rollback
the way ``AsyncSession`` and ``begin()`` behave), the product-id query
(``ProductRepository`` as seen by the scheduler) and, in most tests, the
``PriceService``. Time is never real: the loop pause is observed through a
wrapped ``asyncio.timeout``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.scheduler as scheduler
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.scheduler import price_check_loop
from app.scheduler import run_price_check_pass
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient
from app.services.notification import NotificationService
from app.services.price_service import PriceService

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_WAIT = 5.0  # hang guard only, never a synchronisation delay


class _FakeTransaction:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # ``AsyncSessionTransaction``: commit on a clean exit, rollback otherwise.
        self._session.outcome = "commit" if exc_type is None else "rollback"


class _FakeSession:
    def __init__(self, index: int) -> None:
        self.index = index
        self.closed = False
        self.outcome: str | None = None

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def flush(self) -> None:
        return None


def _price_of(article: int) -> MarketplaceProductData:
    """The result a fake fetch returns for ``article``: unique per article."""
    return MarketplaceProductData(name=f"item {article}", price=article)


class _World:
    """Everything the scheduler touches, with the observations tests need."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ids: list[int],
        *,
        specs: dict[int, tuple[Marketplace, int]] | None = None,
    ) -> None:
        self.ids = ids
        # ``id -> (marketplace, article)``; by default every product is a WB one
        # with an article of its own, so each product is a distinct unit of work.
        self.specs = specs or {i: (Marketplace.wb, 1000 + i) for i in ids}
        self.sessions: list[_FakeSession] = []
        self.select_calls = 0
        self.select_error: Exception | None = None
        self.sessionmaker_error: Exception | None = None
        self.select_done = asyncio.Event()
        world = self

        def sessionmaker() -> _FakeSession:
            if world.sessionmaker_error is not None:
                raise world.sessionmaker_error
            session = _FakeSession(len(world.sessions))
            world.sessions.append(session)
            return session

        self.sessionmaker = cast(async_sessionmaker[AsyncSession], sessionmaker)

        class _FakeProductRepository:
            def __init__(self, session: object) -> None:
                self._session = session

            async def get_all_with_active_alerts(self) -> list[Product]:
                world.select_calls += 1
                world.select_done.set()
                if world.select_error is not None:
                    raise world.select_error
                return [
                    Product(
                        id=i,
                        marketplace=world.specs[i][0],
                        article=world.specs[i][1],
                    )
                    for i in world.ids
                ]

        monkeypatch.setattr(scheduler, "ProductRepository", _FakeProductRepository)

    def open_sessions(self) -> int:
        return sum(1 for session in self.sessions if not session.closed)


type _FetchBody = Callable[
    [Marketplace, list[int]], Awaitable[dict[int, MarketplaceFetchResult] | None]
]


class _FakeService:
    """The three methods the scheduler calls on ``PriceService``.

    ``batch_size`` is a per-marketplace table (default 1: every product is its
    own batch). ``fetch_prices`` records its calls and answers every requested
    article with ``_price_of(article)`` unless ``fetch_body`` returns a dict of
    its own (or raises). ``apply_result`` records the product id, the result and
    the session, then runs the pluggable ``body``.
    """

    def __init__(
        self,
        body: Callable[[int, _FakeSession], Awaitable[None]] | None = None,
        *,
        fetch_body: _FetchBody | None = None,
        sizes: dict[Marketplace, int] | None = None,
        size_errors: dict[Marketplace, Exception] | None = None,
    ) -> None:
        self.checked: list[int] = []
        self.fetches: list[tuple[Marketplace, list[int]]] = []
        self.applied: dict[int, MarketplaceFetchResult] = {}
        self.apply_sessions: list[_FakeSession] = []
        self._body = body
        self._fetch_body = fetch_body
        self._sizes = sizes or {}
        self._size_errors = size_errors or {}

    def batch_size(self, marketplace: Marketplace) -> int:
        if marketplace in self._size_errors:
            raise self._size_errors[marketplace]
        return self._sizes.get(marketplace, 1)

    async def fetch_prices(
        self, marketplace: Marketplace, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        requested = list(articles)
        self.fetches.append((marketplace, requested))
        if self._fetch_body is not None:
            custom = await self._fetch_body(marketplace, requested)
            if custom is not None:
                return custom
        return {article: _price_of(article) for article in requested}

    async def apply_result(
        self, product_id: int, result: MarketplaceFetchResult, session: _FakeSession
    ) -> None:
        self.checked.append(product_id)
        self.applied[product_id] = result
        self.apply_sessions.append(session)
        if self._body is not None:
            await self._body(product_id, session)

    def as_service(self) -> PriceService:
        return cast(PriceService, self)


def _capture() -> tuple[list[Record], int]:
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(_sink, level=0)


async def _run_pass(
    world: _World,
    service: PriceService,
    *,
    concurrency: int = 2,
    stop_event: asyncio.Event | None = None,
) -> None:
    await run_price_check_pass(
        service,
        world.sessionmaker,
        concurrency=concurrency,
        stop_event=stop_event or asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_failing_product_is_rolled_back_logged_with_id_and_does_not_stop_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6(а). Mutation: remove the ``except Exception`` isolation in ``_apply_one``."""
    world = _World(monkeypatch, [101, 102])

    async def body(product_id: int, session: _FakeSession) -> None:
        if product_id == 101:
            raise RuntimeError("boom")

    service = _FakeService(body)
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=1)
    finally:
        logger.remove(sink_id)

    assert service.checked == [101, 102]
    # sessions[0] is the id query; 1 and 2 are products 101 and 102.
    assert world.sessions[1].outcome == "rollback"
    assert world.sessions[2].outcome == "commit"
    errors = [r for r in records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["extra"]["product_id"] == 101


@pytest.mark.asyncio
async def test_naive_triggered_at_isolates_that_product_and_the_rest_are_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6(б), real ``PriceService``: a naive ``triggered_at`` raises ``TypeError``
    inside the cooldown comparison; the pass must survive it."""
    world = _World(monkeypatch, [1, 2])
    products = {
        1: Product(id=1, user_id=1, marketplace=Marketplace.wb, article=1),
        2: Product(id=2, user_id=1, marketplace=Marketplace.wb, article=2),
    }
    for product in products.values():
        product.current_price = 100000
    naive = Alert(
        id=1,
        user_id=1,
        product_id=1,
        target_price=120000,
        direction=AlertDirection.below,
        is_active=True,
        triggered_at=datetime(2020, 1, 1),  # noqa: DTZ001 - deliberately naive
    )
    fresh = Alert(
        id=2,
        user_id=1,
        product_id=2,
        target_price=120000,
        direction=AlertDirection.below,
        is_active=True,
        triggered_at=None,
    )
    alerts = {1: [naive], 2: [fresh]}

    product_repo = MagicMock(spec=ProductRepository)
    product_repo.get_by_id.side_effect = lambda pid: products[pid]
    alert_repo = MagicMock(spec=AlertRepository)
    alert_repo.get_active_by_product.side_effect = lambda pid: alerts[pid]
    user_repo = MagicMock(spec=UserRepository)
    user_repo.get_by_id.return_value = User(id=1, tg_user_id=5, tg_username="u")
    client = MagicMock(spec=BaseMarketplaceClient)
    client.batch_size = 1
    client.get_products_data.side_effect = lambda articles: {
        article: MarketplaceProductData(name="Товар", price=100000)
        for article in articles
    }
    notification = MagicMock(spec=NotificationService)
    service = PriceService(
        notification_service=cast(NotificationService, notification),
        product_repo_factory=lambda _s: cast(ProductRepository, product_repo),
        alert_repo_factory=lambda _s: cast(AlertRepository, alert_repo),
        user_repo_factory=lambda _s: cast(UserRepository, user_repo),
        client_factory=lambda _m, _h: cast(BaseMarketplaceClient, client),
        http_client=cast(httpx.AsyncClient, MagicMock(spec=httpx.AsyncClient)),
        alert_cooldown_seconds=3600,
    )
    records, sink_id = _capture()
    try:
        await _run_pass(world, service, concurrency=1)
    finally:
        logger.remove(sink_id)

    assert world.sessions[1].outcome == "rollback"
    assert world.sessions[2].outcome == "commit"
    notification.send_alert.assert_called_once()
    errors = [r for r in records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["extra"]["product_id"] == 1
    exception = errors[0]["exception"]
    assert exception is not None
    assert exception.type is TypeError


@pytest.mark.asyncio
async def test_parallel_checks_reach_but_never_exceed_the_concurrency_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6(в). Mutations: drop the cap (7 workers), cap at ``concurrency + 1``, or
    serialise (max would be 1)."""
    world = _World(monkeypatch, list(range(1, 8)))
    running = 0
    peak = 0

    async def body(product_id: int, session: _FakeSession) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        for _ in range(5):
            await asyncio.sleep(0)  # yield to the other workers, no real delay
        running -= 1

    service = _FakeService(body)
    await _run_pass(world, service.as_service(), concurrency=3)

    assert sorted(service.checked) == list(range(1, 8))
    assert peak == 3


@pytest.mark.asyncio
async def test_id_query_session_is_closed_before_the_first_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6(г). Mutation: run the checks inside the ``async with`` of the id query."""
    world = _World(monkeypatch, [1, 2])
    closed_at_first_check: list[bool] = []

    async def body(product_id: int, session: _FakeSession) -> None:
        closed_at_first_check.append(world.sessions[0].closed)
        assert session is not world.sessions[0]

    service = _FakeService(body)
    await _run_pass(world, service.as_service())

    assert closed_at_first_check == [True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["select", "sessionmaker"])
async def test_id_query_failure_is_logged_and_pass_returns_without_checks(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """6(д). Mutation: drop the ``except`` around the id query (the loop dies)."""
    world = _World(monkeypatch, [1])
    if failure == "select":
        world.select_error = ConnectionError("db down")
    else:
        world.sessionmaker_error = ConnectionError("db down")
    service = _FakeService()
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service())
    finally:
        logger.remove(sink_id)

    assert service.checked == []
    assert [r["level"].name for r in records] == ["ERROR"]
    exception = records[0]["exception"]
    assert exception is not None
    assert exception.type is ConnectionError


@pytest.mark.asyncio
async def test_stop_event_set_mid_pass_prevents_starting_remaining_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6(е). Mutation: drop the ``stop_event`` check in the worker loop."""
    world = _World(monkeypatch, [1, 2, 3, 4, 5])
    stop_event = asyncio.Event()

    async def body(product_id: int, session: _FakeSession) -> None:
        stop_event.set()

    service = _FakeService(body)
    await _run_pass(world, service.as_service(), concurrency=1, stop_event=stop_event)

    assert service.checked == [1]


@pytest.mark.asyncio
async def test_cancellation_during_a_check_propagates_and_is_not_counted_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6(ж), convention 23. Mutation: ``except BaseException`` in ``_apply_one``
    (the cancel would be logged as a product failure and the next product
    started)."""
    world = _World(monkeypatch, [1, 2, 3])
    started = asyncio.Event()

    async def body(product_id: int, session: _FakeSession) -> None:
        started.set()
        if product_id == 1:  # later products return, so a swallowed cancel cannot hang
            await asyncio.Event().wait()

    service = _FakeService(body)
    records, sink_id = _capture()
    task = asyncio.create_task(_run_pass(world, service.as_service(), concurrency=1))
    try:
        await asyncio.wait_for(started.wait(), _WAIT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.remove(sink_id)

    assert task.cancelled()
    assert service.checked == [1]
    assert world.sessions[1].outcome == "rollback"
    assert world.sessions[1].closed is True
    assert [r for r in records if r["level"].name in {"ERROR", "INFO"}] == []


def _summaries(records: list[Record]) -> list[Record]:
    """Pass-summary records: the only ones carrying a ``products`` field."""
    return [r for r in records if "products" in r["extra"]]


async def _raise_unrequested_cancel_on_first(
    product_id: int, session: _FakeSession
) -> None:
    # Raised directly: nobody called ``task.cancel()``, so ``cancelling() == 0``.
    if product_id == 1:
        raise asyncio.CancelledError


async def _request_cancel_on_first(product_id: int, session: _FakeSession) -> None:
    if product_id == 1:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)  # the pending cancel surfaces at this await


@pytest.mark.asyncio
async def test_unrequested_cancellation_in_a_check_does_not_stop_the_remaining_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (а, б, г). Mutation: ``cancelling()`` condition -> always re-raise."""
    world = _World(monkeypatch, [1, 2, 3])
    service = _FakeService(_raise_unrequested_cancel_on_first)
    task = asyncio.create_task(_run_pass(world, service.as_service(), concurrency=1))
    await asyncio.wait_for(task, _WAIT)

    assert not task.cancelled()
    assert service.checked == [1, 2, 3]
    # sessions[0] is the id query; 1..3 are products 1..3.
    assert world.sessions[1].outcome == "rollback"
    assert world.sessions[1].closed is True
    assert world.sessions[2].outcome == "commit"
    assert world.sessions[3].outcome == "commit"


@pytest.mark.asyncio
async def test_unrequested_cancellation_is_logged_once_as_error_with_product_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 (в). Mutation: drop the ``logger`` call in the ``CancelledError`` branch."""
    world = _World(monkeypatch, [1, 2, 3])
    records, sink_id = _capture()
    try:
        await asyncio.wait_for(
            _run_pass(
                world,
                _FakeService(_raise_unrequested_cancel_on_first).as_service(),
                concurrency=1,
            ),
            _WAIT,
        )
    finally:
        logger.remove(sink_id)

    errors = [r for r in records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["extra"]["product_id"] == 1
    exception = errors[0]["exception"]
    assert exception is not None
    assert exception.type is asyncio.CancelledError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ids", "concurrency", "expected"),
    [
        ([1, 2, 3], 1, {"products": 3, "checked": 3, "failures": 1}),
        ([1, 2, 3, 4, 5], 2, {"products": 5, "checked": 5, "failures": 1}),
    ],
)
async def test_unrequested_cancellation_is_counted_as_a_failure_in_an_info_summary(
    monkeypatch: pytest.MonkeyPatch,
    ids: list[int],
    concurrency: int,
    expected: dict[str, int],
) -> None:
    """AC1 (д). Mutation: ``checked += 1`` only on success; or the branch not
    counting the failure."""
    world = _World(monkeypatch, ids)
    service = _FakeService(_raise_unrequested_cancel_on_first)
    records, sink_id = _capture()
    try:
        await asyncio.wait_for(
            _run_pass(world, service.as_service(), concurrency=concurrency), _WAIT
        )
    finally:
        logger.remove(sink_id)

    assert sorted(service.checked) == ids
    summaries = _summaries(records)
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "INFO"
    assert summaries[0]["extra"] == expected


@pytest.mark.asyncio
async def test_requested_cancellation_inside_a_check_ends_the_worker_without_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 (б), cell C4. Mutation: ``cancelling()`` condition -> always a product
    failure (product 2 would be checked and an ``ERROR`` with ``product_id``
    logged)."""
    world = _World(monkeypatch, [1, 2, 3])
    service = _FakeService(_request_cancel_on_first)
    records, sink_id = _capture()
    try:
        await asyncio.wait_for(
            _run_pass(world, service.as_service(), concurrency=1), _WAIT
        )
    finally:
        logger.remove(sink_id)

    assert service.checked == [1]
    assert world.sessions[1].outcome == "rollback"
    assert world.sessions[1].closed is True
    assert len(_summaries(records)) == 1
    assert [
        r for r in records if r["level"].name == "ERROR" and "product_id" in r["extra"]
    ] == []


@pytest.mark.asyncio
async def test_pass_with_a_vanished_worker_logs_one_error_summary_and_no_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 (г), cell C4. Mutation: summary level always ``INFO``."""
    world = _World(monkeypatch, [1, 2, 3])
    records, sink_id = _capture()
    try:
        await asyncio.wait_for(
            _run_pass(
                world,
                _FakeService(_request_cancel_on_first).as_service(),
                concurrency=1,
            ),
            _WAIT,
        )
    finally:
        logger.remove(sink_id)

    summaries = _summaries(records)
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "ERROR"
    assert summaries[0]["extra"] == {"products": 3, "checked": 0, "failures": 0}
    assert [r for r in records if r["level"].name == "INFO"] == []


@pytest.mark.asyncio
async def test_stopped_pass_logs_info_summary_counting_only_started_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 (в). Mutations: ``checked`` counted as ``products``; level ``ERROR``
    when the pass was stopped."""
    world = _World(monkeypatch, [1, 2, 3, 4, 5])
    stop_event = asyncio.Event()

    async def body(product_id: int, session: _FakeSession) -> None:
        stop_event.set()

    records, sink_id = _capture()
    try:
        await _run_pass(
            world, _FakeService(body).as_service(), concurrency=1, stop_event=stop_event
        )
    finally:
        logger.remove(sink_id)

    summaries = _summaries(records)
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "INFO"
    assert summaries[0]["extra"] == {"products": 5, "checked": 1, "failures": 0}


@pytest.mark.asyncio
async def test_pass_logs_one_info_with_product_and_failure_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch, [1, 2, 3])

    async def body(product_id: int, session: _FakeSession) -> None:
        if product_id == 2:
            raise RuntimeError("boom")

    records, sink_id = _capture()
    try:
        await _run_pass(world, _FakeService(body).as_service(), concurrency=2)
    finally:
        logger.remove(sink_id)

    infos = [r for r in records if r["level"].name == "INFO"]
    assert len(infos) == 1
    assert infos[0]["extra"] == {"products": 3, "checked": 3, "failures": 1}


@pytest.mark.asyncio
async def test_pass_over_no_products_checks_nothing_and_logs_zero_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch, [])
    service = _FakeService()
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service())
    finally:
        logger.remove(sink_id)

    assert service.checked == []
    assert len(world.sessions) == 1
    assert [r["extra"] for r in records if r["level"].name == "INFO"] == [
        {"products": 0, "checked": 0, "failures": 0}
    ]


# --- loop (AC7) --------------------------------------------------------------


class _PauseProbe:
    """Wraps ``asyncio.timeout`` so a test sees the pause and may shorten it."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, real_delay: float | None
    ) -> None:
        self.delays: list[float | None] = []
        self.entered = asyncio.Event()
        real_timeout = asyncio.timeout
        probe = self

        class _Wrapped:
            def __init__(self, delay: float | None) -> None:
                probe.delays.append(delay)
                self._inner = real_timeout(delay if real_delay is None else real_delay)

            async def __aenter__(self) -> Any:
                probe.entered.set()
                return await self._inner.__aenter__()

            async def __aexit__(self, *exc_info: Any) -> None:
                await self._inner.__aexit__(*exc_info)

        monkeypatch.setattr(asyncio, "timeout", _Wrapped)


@pytest.mark.asyncio
async def test_loop_runs_first_pass_at_once_and_stop_interrupts_the_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7. Mutations: sleep before the first pass; wait the pause with
    ``asyncio.sleep`` instead of ``stop_event.wait`` (the interval is an hour,
    so a hang shows up as the ``wait_for`` timeout)."""
    world = _World(monkeypatch, [])
    probe = _PauseProbe(monkeypatch, real_delay=None)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        price_check_loop(
            _FakeService().as_service(),
            world.sessionmaker,
            interval_seconds=3600,
            concurrency=2,
            stop_event=stop_event,
        )
    )

    await asyncio.wait_for(probe.entered.wait(), _WAIT)
    assert world.select_calls == 1
    stop_event.set()
    await asyncio.wait_for(task, _WAIT)

    assert probe.delays == [3600]
    assert world.select_calls == 1


@pytest.mark.asyncio
async def test_loop_runs_another_pass_after_each_pause_with_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: ``break`` instead of ``continue`` on the pause timeout."""
    world = _World(monkeypatch, [])
    probe = _PauseProbe(monkeypatch, real_delay=0.001)
    stop_event = asyncio.Event()
    real_select_done = world.select_done

    async def stop_after_three_passes() -> None:
        while world.select_calls < 3:
            real_select_done.clear()
            await real_select_done.wait()
        stop_event.set()

    watcher = asyncio.create_task(stop_after_three_passes())
    await asyncio.wait_for(
        price_check_loop(
            _FakeService().as_service(),
            world.sessionmaker,
            interval_seconds=4242,
            concurrency=2,
            stop_event=stop_event,
        ),
        _WAIT,
    )
    await watcher

    assert world.select_calls >= 3
    assert set(probe.delays) == {4242}


@pytest.mark.asyncio
async def test_loop_with_stop_event_already_set_runs_no_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch, [1])
    stop_event = asyncio.Event()
    stop_event.set()

    await asyncio.wait_for(
        price_check_loop(
            _FakeService().as_service(),
            world.sessionmaker,
            interval_seconds=3600,
            concurrency=2,
            stop_event=stop_event,
        ),
        _WAIT,
    )

    assert world.select_calls == 0


def test_loop_is_a_coroutine_function_and_the_old_name_is_gone() -> None:
    assert inspect.iscoroutinefunction(price_check_loop)
    assert not hasattr(scheduler, "price_price_check_loop")


# --- batches (PAB-081 AC4) ---------------------------------------------------


def _assert_summary(
    records: list[Record], *, products: int, checked: int, failures: int, level: str
) -> None:
    """Exactly one summary with the given counts, the level, and the invariant."""
    summaries = _summaries(records)
    assert len(summaries) == 1
    assert summaries[0]["level"].name == level
    assert summaries[0]["extra"] == {
        "products": products,
        "checked": checked,
        "failures": failures,
    }
    assert failures <= checked <= products


def _errors(records: list[Record]) -> list[Record]:
    return [r for r in records if r["level"].name == "ERROR" and not _summaries([r])]


@pytest.mark.asyncio
async def test_250_wb_products_are_fetched_in_three_batches_and_applied_one_by_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(а). Mutations: ``batch_size`` ignored (250 fetches); one session for the
    whole batch; ``apply_result`` skipped for the tail batch."""
    ids = list(range(1, 251))
    world = _World(monkeypatch, ids)
    service = _FakeService(sizes={Marketplace.wb: 100})
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=1)
    finally:
        logger.remove(sink_id)

    assert [(m, len(a)) for m, a in service.fetches] == [
        (Marketplace.wb, 100),
        (Marketplace.wb, 100),
        (Marketplace.wb, 50),
    ]
    requested = [article for _, articles in service.fetches for article in articles]
    assert sorted(requested) == [1000 + i for i in ids]
    assert sorted(service.checked) == ids
    # Each product got the result of its own article.
    assert all(service.applied[i] == _price_of(1000 + i) for i in ids)
    # One id-query session plus one session per product, each a committed
    # transaction and none shared.
    assert len(world.sessions) == 251
    assert len({id(s) for s in service.apply_sessions}) == 250
    assert all(s.outcome == "commit" and s.closed for s in service.apply_sessions)
    _assert_summary(records, products=250, checked=250, failures=0, level="INFO")


@pytest.mark.asyncio
async def test_products_sharing_an_article_are_fetched_once_and_both_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(б). Mutation: no merge of equal articles (the article is requested twice)."""
    world = _World(
        monkeypatch,
        [1, 2, 3],
        specs={
            1: (Marketplace.wb, 777),
            2: (Marketplace.wb, 777),
            3: (Marketplace.wb, 888),
        },
    )
    service = _FakeService(sizes={Marketplace.wb: 100})
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=1)
    finally:
        logger.remove(sink_id)

    assert service.fetches == [(Marketplace.wb, [777, 888])]
    assert service.applied == {1: _price_of(777), 2: _price_of(777), 3: _price_of(888)}
    # ``products`` counts products, not articles.
    _assert_summary(records, products=3, checked=3, failures=0, level="INFO")


@pytest.mark.asyncio
async def test_equal_article_numbers_of_different_marketplaces_are_not_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(а). Mutation: group by article only (one fetch, one marketplace lost)."""
    world = _World(
        monkeypatch,
        [1, 2, 3],
        specs={
            1: (Marketplace.wb, 5),
            2: (Marketplace.ozon, 5),
            3: (Marketplace.wb, 6),
        },
    )
    service = _FakeService(sizes={Marketplace.wb: 100, Marketplace.ozon: 1})
    await _run_pass(world, service.as_service(), concurrency=1)

    assert service.fetches == [(Marketplace.wb, [5, 6]), (Marketplace.ozon, [5])]
    assert sorted(service.checked) == [1, 2, 3]


@pytest.mark.asyncio
async def test_ozon_products_are_single_article_batches_within_the_concurrency_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(в). Mutations: Ozon batched together; the cap dropped or off by one."""
    ids = list(range(1, 8))
    world = _World(monkeypatch, ids, specs={i: (Marketplace.ozon, 50 + i) for i in ids})
    running = 0
    peak = 0

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        for _ in range(5):
            await asyncio.sleep(0)  # yield to the other workers, no real delay
        running -= 1
        return None

    service = _FakeService(fetch_body=fetch_body, sizes={Marketplace.ozon: 1})
    await _run_pass(world, service.as_service(), concurrency=2)

    assert sorted(len(a) for _, a in service.fetches) == [1] * 7
    assert peak == 2
    assert sorted(service.checked) == ids


@pytest.mark.asyncio
async def test_failed_batch_fetch_fails_every_product_of_that_batch_and_logs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(г). Mutations: the failure not counted; one log record per product; a
    failed batch aborting the neighbouring one."""
    world = _World(monkeypatch, [1, 2, 3, 4])  # articles 1001..1004, batches of 2

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        if 1001 in articles:
            raise RuntimeError("boom")
        return None

    service = _FakeService(fetch_body=fetch_body, sizes={Marketplace.wb: 2})
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=1)
    finally:
        logger.remove(sink_id)

    assert service.checked == [3, 4]
    # Id query plus the two applied products only: nothing opened for 1 and 2.
    assert len(world.sessions) == 3
    errors = _errors(records)
    assert len(errors) == 1
    assert errors[0]["extra"] == {"marketplace": "wb", "batch_size": 2}
    _assert_summary(records, products=4, checked=4, failures=2, level="INFO")


@pytest.mark.asyncio
async def test_unrequested_cancellation_in_fetch_fails_the_batch_and_the_worker_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(е). Mutation: ``cancelling()`` condition in ``fetch`` -> always re-raise
    (the worker dies silently, the next batch is never fetched)."""
    world = _World(monkeypatch, [1, 2, 3])

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        if articles == [1001]:
            raise asyncio.CancelledError  # nobody called ``cancel()``
        return None

    service = _FakeService(fetch_body=fetch_body)
    records, sink_id = _capture()
    try:
        await asyncio.wait_for(
            _run_pass(world, service.as_service(), concurrency=1), _WAIT
        )
    finally:
        logger.remove(sink_id)

    assert service.checked == [2, 3]
    errors = _errors(records)
    assert len(errors) == 1
    assert errors[0]["extra"] == {"marketplace": "wb", "batch_size": 1}
    _assert_summary(records, products=3, checked=3, failures=1, level="INFO")


@pytest.mark.asyncio
async def test_requested_cancellation_inside_fetch_ends_the_worker_without_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(е). Mutation: ``cancelling()`` condition in ``fetch`` -> always a batch
    failure (batch 2 would be fetched and an ``ERROR`` logged)."""
    world = _World(monkeypatch, [1, 2, 3])

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)  # the pending cancel surfaces at this await
        return None

    service = _FakeService(fetch_body=fetch_body)
    records, sink_id = _capture()
    try:
        await asyncio.wait_for(
            _run_pass(world, service.as_service(), concurrency=1), _WAIT
        )
    finally:
        logger.remove(sink_id)

    assert len(service.fetches) == 1
    assert service.checked == []
    assert _errors(records) == []
    _assert_summary(records, products=3, checked=0, failures=0, level="ERROR")


@pytest.mark.asyncio
async def test_cancelling_the_pass_during_a_fetch_propagates_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(е), convention 23. Mutation: ``except BaseException`` around the fetch."""
    world = _World(monkeypatch, [1, 2])
    started = asyncio.Event()

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        started.set()
        await asyncio.Event().wait()
        return None

    service = _FakeService(fetch_body=fetch_body)
    records, sink_id = _capture()
    task = asyncio.create_task(_run_pass(world, service.as_service(), concurrency=1))
    try:
        await asyncio.wait_for(started.wait(), _WAIT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        logger.remove(sink_id)

    assert task.cancelled()
    assert len(service.fetches) == 1
    assert [r for r in records if r["level"].name in {"ERROR", "INFO"}] == []


@pytest.mark.asyncio
async def test_failing_apply_of_one_product_leaves_its_batch_neighbours_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(д). Mutations: failure not counted; failure aborting the rest of the
    batch; one transaction for the whole batch."""
    world = _World(monkeypatch, [1, 2, 3])

    async def body(product_id: int, session: _FakeSession) -> None:
        if product_id == 2:
            raise RuntimeError("boom")

    service = _FakeService(body, sizes={Marketplace.wb: 100})
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=1)
    finally:
        logger.remove(sink_id)

    assert len(service.fetches) == 1
    assert service.checked == [1, 2, 3]
    assert [s.outcome for s in service.apply_sessions] == [
        "commit",
        "rollback",
        "commit",
    ]
    errors = _errors(records)
    assert len(errors) == 1
    assert errors[0]["extra"]["product_id"] == 2
    _assert_summary(records, products=3, checked=3, failures=1, level="INFO")


@pytest.mark.asyncio
async def test_article_missing_from_the_fetch_result_fails_only_its_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing-key branch. Mutation: the lookup moved out of the ``try`` (the
    ``KeyError`` kills the worker and the rest of the batch is lost)."""
    world = _World(monkeypatch, [1, 2, 3])

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        return {a: _price_of(a) for a in articles if a != 1002}

    service = _FakeService(fetch_body=fetch_body, sizes={Marketplace.wb: 100})
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=1)
    finally:
        logger.remove(sink_id)

    assert service.checked == [1, 3]  # ``apply_result`` is never reached for 2
    errors = _errors(records)
    assert len(errors) == 1
    assert errors[0]["extra"]["product_id"] == 2
    exception = errors[0]["exception"]
    assert exception is not None
    assert exception.type is KeyError
    _assert_summary(records, products=3, checked=3, failures=1, level="INFO")


@pytest.mark.asyncio
async def test_batch_size_failure_fails_that_marketplace_and_the_others_are_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(г) analogue. Mutations: ``batch_size`` exception unhandled (the pass
    dies); the skipped products not counted; no log record."""
    world = _World(
        monkeypatch,
        [1, 2, 3],
        specs={
            1: (Marketplace.ozon, 10),
            2: (Marketplace.wb, 20),
            3: (Marketplace.ozon, 30),
        },
    )
    service = _FakeService(
        sizes={Marketplace.wb: 100},
        size_errors={Marketplace.ozon: LookupError("no client")},
    )
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=2)
    finally:
        logger.remove(sink_id)

    assert service.fetches == [(Marketplace.wb, [20])]
    assert service.checked == [2]
    errors = _errors(records)
    assert len(errors) == 1
    assert errors[0]["extra"] == {"marketplace": "ozon"}
    exception = errors[0]["exception"]
    assert exception is not None
    assert exception.type is LookupError
    _assert_summary(records, products=3, checked=3, failures=2, level="INFO")


@pytest.mark.asyncio
async def test_pass_where_every_batch_size_fails_starts_no_worker_and_counts_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero batches. Mutation: ``min(concurrency, len(batches))`` -> ``concurrency``
    (idle workers are harmless, but the summary base ``checked`` must still be
    the skipped products)."""
    world = _World(monkeypatch, [1, 2])
    service = _FakeService(size_errors={Marketplace.wb: LookupError("no client")})
    records, sink_id = _capture()
    try:
        await _run_pass(world, service.as_service(), concurrency=3)
    finally:
        logger.remove(sink_id)

    assert service.fetches == []
    _assert_summary(records, products=2, checked=2, failures=2, level="INFO")


@pytest.mark.asyncio
async def test_no_session_is_open_during_fetch_and_exactly_one_during_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(з). Mutation: fetch inside the apply session (open count 1 at fetch)."""
    world = _World(monkeypatch, [1, 2, 3])
    open_at_fetch: list[int] = []
    open_at_apply: list[int] = []

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        open_at_fetch.append(world.open_sessions())
        return None

    async def body(product_id: int, session: _FakeSession) -> None:
        open_at_apply.append(world.open_sessions())

    service = _FakeService(body, fetch_body=fetch_body, sizes={Marketplace.wb: 2})
    await _run_pass(world, service.as_service(), concurrency=1)

    assert open_at_fetch == [0, 0]  # two batches
    assert open_at_apply == [1, 1, 1]  # positive control: the counter does see one


@pytest.mark.asyncio
async def test_stop_event_set_mid_batch_applies_no_more_products_and_takes_no_new_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(ж). Mutations: drop the per-product ``stop_event`` check; drop the
    per-batch one."""
    world = _World(monkeypatch, [1, 2, 3, 4, 5])
    stop_event = asyncio.Event()

    async def body(product_id: int, session: _FakeSession) -> None:
        stop_event.set()

    service = _FakeService(body, sizes={Marketplace.wb: 3})
    records, sink_id = _capture()
    try:
        await _run_pass(
            world, service.as_service(), concurrency=1, stop_event=stop_event
        )
    finally:
        logger.remove(sink_id)

    assert len(service.fetches) == 1
    assert service.checked == [1]
    _assert_summary(records, products=5, checked=1, failures=0, level="INFO")


@pytest.mark.asyncio
async def test_stop_event_set_during_a_fetch_applies_nothing_from_that_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4(ж). Mutation: the ``stop_event`` check only before taking a batch."""
    world = _World(monkeypatch, [1, 2])
    stop_event = asyncio.Event()

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        stop_event.set()
        return None

    service = _FakeService(fetch_body=fetch_body, sizes={Marketplace.wb: 100})
    records, sink_id = _capture()
    try:
        await _run_pass(
            world, service.as_service(), concurrency=1, stop_event=stop_event
        )
    finally:
        logger.remove(sink_id)

    assert len(service.fetches) == 1
    assert service.checked == []
    _assert_summary(records, products=2, checked=0, failures=0, level="INFO")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "exception_type"),
    [
        (RuntimeError("boom"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
    ids=["exception", "unrequested_cancellation"],
)
async def test_batch_fetch_failure_record_carries_the_exception(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    exception_type: type[BaseException],
) -> None:
    """4(г), PAB-073 convention: the failure record names the exception type, as
    the per-product record does. Mutation: none needed - ``logger.exception`` in
    ``fetch`` runs after the ``except`` block, so the record has no exception."""
    world = _World(monkeypatch, [1])

    async def fetch_body(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        raise failure

    service = _FakeService(fetch_body=fetch_body)
    records, sink_id = _capture()
    try:
        await asyncio.wait_for(
            _run_pass(world, service.as_service(), concurrency=1), _WAIT
        )
    finally:
        logger.remove(sink_id)

    errors = _errors(records)
    assert len(errors) == 1
    exception = errors[0]["exception"]
    assert exception is not None
    assert exception.type is exception_type
