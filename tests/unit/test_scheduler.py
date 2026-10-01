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


class _World:
    """Everything the scheduler touches, with the observations tests need."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, ids: list[int]) -> None:
        self.ids = ids
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
                return [Product(id=i) for i in world.ids]

        monkeypatch.setattr(scheduler, "ProductRepository", _FakeProductRepository)


class _FakeService:
    """``check_product`` with a pluggable body; records ids and the session."""

    def __init__(
        self,
        body: Callable[[int, _FakeSession], Awaitable[None]] | None = None,
    ) -> None:
        self.checked: list[int] = []
        self._body = body

    async def check_product(self, product_id: int, session: _FakeSession) -> None:
        self.checked.append(product_id)
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
    """6(а). Mutation: remove the ``except Exception`` isolation in ``_check_one``."""
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
    client.get_product_data.return_value = MarketplaceProductData(
        name="Товар", price=100000
    )
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
    """6(ж), convention 23. Mutation: ``except BaseException`` in ``_check_one``
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
    assert infos[0]["extra"]["products"] == 3
    assert infos[0]["extra"]["failures"] == 1


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
        {"products": 0, "failures": 0}
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
