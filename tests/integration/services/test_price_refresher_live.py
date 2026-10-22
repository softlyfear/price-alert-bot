"""``PriceRefresher`` against a live PostgreSQL (PAB-100 AC2-AC6).

The unit tests of ``tests/unit/services/test_price_refresher.py`` replace the
database; this module checks what only a real one can: the SQL owner scoping,
that no pooled connection is held while a marketplace is being asked, that a
failed write rolls back only its own product, and that what is applied stays
committed when the deadline hits.

**Isolation.** A refresh opens sessions of its own, so the usual rolled-back
``db_session`` transaction cannot hold the data: rows are committed through a
private engine, and the fixture deletes the test users (their products and
alerts go with them by ``ON DELETE CASCADE``) before and after each test. All
test users live in one reserved ``tg_user_id`` range.

Replaced boundaries: the marketplace clients (a stub that counts, overlaps and
can answer or hang per article), the Bot API (the fake session of
``tests/unit/conftest.py``) and the clock of ``app.services.price_service`` and
``app.services.notification`` (a literal instant, so every expected value is
written out by hand).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Iterator
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import NamedTuple
from typing import cast

import httpx
import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup
from loguru import logger
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import QueuePool

import app.services.notification as notification_module
import app.services.price_service as price_service_module
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
from app.services.notification import NotificationService
from app.services.price_service import PriceRefresher
from app.services.price_service import PriceService
from app.services.price_service import RefreshFailure
from app.services.price_service import RefreshGone
from app.services.price_service import RefreshLimiter
from app.services.price_service import RefreshReport
from tests.integration.repositories.conftest import make_alert
from tests.integration.repositories.conftest import make_product
from tests.integration.repositories.conftest import make_user
from tests.unit.conftest import FakeTelegramSession
from tests.unit.conftest import build_fake_bot

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_NOW = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
_OLD_CHECK = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_OLD_STAMP = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
_START_PRICE = 10_000
_NEW_PRICE = 8_000
_COOLDOWN = 3_600
# No other module of tests/integration uses 650_000_000-650_000_999 (checked by grep);
# the fixture deletes this whole range, so it must stay private to this module.
_TG_A = 650_000_001
_TG_B = 650_000_002
_TG_RANGE = (650_000_000, 650_000_999)
_SHORT_DEADLINE = 0.05
_HANG_GUARD = 10.0  # only stops a hung test, never synchronises one
_SEND_FAILURE = TelegramNetworkError(method=cast(Any, None), message="down")


class _FrozenDatetime:
    """Stands in for the ``datetime`` name of the service modules."""

    @staticmethod
    def now(tz: object = None) -> datetime:
        return _NOW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(price_service_module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(notification_module, "datetime", _FrozenDatetime)


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """A private engine whose pool the tests can read; test rows are removed."""
    created = create_async_engine(
        database_url, pool_size=5, max_overflow=0, pool_timeout=5
    )

    async def purge() -> None:
        async with created.begin() as connection:
            await connection.execute(
                delete(User).where(User.tg_user_id.between(*_TG_RANGE))
            )

    try:
        await purge()
        yield created
    finally:
        try:
            await purge()
        finally:
            await created.dispose()


@pytest.fixture
def log_records() -> Iterator[list[Record]]:
    """Collect every loguru record emitted during the test."""
    records: list[Record] = []

    def sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(sink, level="DEBUG")
    yield records
    logger.remove(sink_id)


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def _checked_out(engine: AsyncEngine) -> int:
    return cast(QueuePool, engine.pool).checkedout()


class _Tally:
    """What the marketplace stubs saw, shared by both of them."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.calls: list[tuple[Marketplace, list[int]]] = []
        self.checked_out_at_call: list[int] = []
        self.answers: dict[int, MarketplaceFetchResult] = {}
        self.hang_for: set[Marketplace] = set()
        self.first_call = asyncio.Event()
        self.active = 0
        self.peak = 0

    async def serve(
        self, marketplace: Marketplace, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        asked = list(articles)
        self.calls.append((marketplace, asked))
        self.first_call.set()
        self.checked_out_at_call.append(_checked_out(self._engine))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if marketplace in self.hang_for:
                await asyncio.Event().wait()
            for _ in range(5):  # let the other batches overlap with this one
                await asyncio.sleep(0)
            return {
                a: self.answers.get(
                    a, MarketplaceProductData(name="Item", price=_NEW_PRICE)
                )
                for a in asked
            }
        finally:
            self.active -= 1


class _WbStub(BaseMarketplaceClient):
    batch_size: ClassVar[int] = 100

    def __init__(self, tally: _Tally) -> None:
        self._tally = tally

    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        return (await self.get_products_data([article]))[article]

    async def get_products_data(
        self, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        return await self._tally.serve(Marketplace.wb, articles)


class _OzonStub(_WbStub):
    batch_size: ClassVar[int] = 1

    async def get_products_data(
        self, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        return await self._tally.serve(Marketplace.ozon, articles)


class _FailingApply(PriceService):
    """The real service, except that one product's write fails after it flushed."""

    fail_for: int | None = None
    stall_past: float | None = None
    """If set, the first write waits (after notifying, before commit) until this
    loop time is reached; it models the deadline hitting inside a write."""

    async def apply_result(
        self, product_id: int, result: MarketplaceFetchResult, session: AsyncSession
    ) -> None:
        await super().apply_result(product_id, result, session)
        if self.stall_past is not None:
            reached = asyncio.Event()
            asyncio.get_running_loop().call_at(self.stall_past, reached.set)
            self.stall_past = None
            await reached.wait()
        if product_id == self.fail_for:
            raise RuntimeError("boom after the flush")


class _Rig(NamedTuple):
    refresher: PriceRefresher
    tally: _Tally
    bot_requests: list[TelegramMethod[Any]]
    service: _FailingApply


def _rig(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    concurrency: int = 5,
    deadline: float = 20.0,
    send_failures: int = 0,
) -> _Rig:
    tally = _Tally(engine)
    wb, ozon = _WbStub(tally), _OzonStub(tally)
    failures_left = [send_failures]

    async def respond(method: TelegramMethod[Any]) -> Any:
        if isinstance(method, SendMessage) and failures_left[0] > 0:
            failures_left[0] -= 1
            raise _SEND_FAILURE
        return True

    bot = build_fake_bot(responder=respond)
    notification = NotificationService(
        bot, alert_keyboard=lambda _id: InlineKeyboardMarkup(inline_keyboard=[])
    )
    service = _FailingApply(
        notification_service=notification,
        product_repo_factory=ProductRepository,
        alert_repo_factory=AlertRepository,
        user_repo_factory=UserRepository,
        client_factory=lambda marketplace, _http: (
            wb if marketplace is Marketplace.wb else ozon
        ),
        http_client=httpx.AsyncClient(),
        alert_cooldown_seconds=_COOLDOWN,
    )
    refresher = PriceRefresher(
        service,
        sessionmaker,
        ProductRepository,
        UserRepository,
        concurrency=concurrency,
        limiter=RefreshLimiter(60.0, lambda: 0.0),
        deadline_seconds=deadline,
    )
    session = cast(FakeTelegramSession, bot.session)
    return _Rig(refresher, tally, session.requests, service)


async def _seed_user(sessionmaker: async_sessionmaker[AsyncSession], tg: int) -> int:
    async with sessionmaker() as session, session.begin():
        return (await make_user(session, tg_user_id=tg)).id


async def _seed_products(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: int,
    count: int,
    marketplace: Marketplace = Marketplace.wb,
) -> list[int]:
    ids: list[int] = []
    async with sessionmaker() as session, session.begin():
        for _ in range(count):
            product = await make_product(
                session,
                user_id=user_id,
                marketplace=marketplace,
                current_price=_START_PRICE,
            )
            product.last_checked_at = _OLD_CHECK
            product.price_fetched_at = _OLD_STAMP
            ids.append(product.id)
    return ids


class _Row(NamedTuple):
    current_price: int
    previous_price: int | None
    last_checked_at: datetime | None
    price_fetched_at: datetime | None
    article: int


async def _row(sessionmaker: async_sessionmaker[AsyncSession], product_id: int) -> _Row:
    async with sessionmaker() as session:
        found = (
            await session.execute(
                select(
                    Product.current_price,
                    Product.previous_price,
                    Product.last_checked_at,
                    Product.price_fetched_at,
                    Product.article,
                ).where(Product.id == product_id)
            )
        ).one()
    return _Row(*found)


_UNTOUCHED = (_START_PRICE, None, _OLD_CHECK, _OLD_STAMP)


def _state(row: _Row) -> tuple[int, int | None, datetime | None, datetime | None]:
    return row[:4]


_REFRESHED = (_NEW_PRICE, _START_PRICE, _NOW, _NOW)


def _consistent(report: RefreshReport) -> RefreshReport:
    assert report.updated + sum(report.failures.values()) == report.total
    return report


# --- AC2: the owner is checked in SQL ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["foreign", "missing"])
async def test_refresh_product_of_a_foreign_or_missing_id_is_gone_and_changes_nothing(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    target: str,
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    user_b = await _seed_user(sessionmaker, _TG_B)
    (own,) = await _seed_products(sessionmaker, user_a, 1)
    (foreign,) = await _seed_products(sessionmaker, user_b, 1)
    rig = _rig(engine, sessionmaker)
    asked = foreign if target == "foreign" else 2_000_000_000

    result = await rig.refresher.refresh_product(_TG_A, asked)

    assert result == RefreshGone()
    assert rig.tally.calls == []
    assert _state(await _row(sessionmaker, foreign)) == _UNTOUCHED
    assert _state(await _row(sessionmaker, own)) == _UNTOUCHED


@pytest.mark.asyncio
async def test_refresh_list_touches_only_the_callers_products(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    user_b = await _seed_user(sessionmaker, _TG_B)
    a_ids = [
        *await _seed_products(sessionmaker, user_a, 1),
        *await _seed_products(sessionmaker, user_a, 1, Marketplace.ozon),
    ]
    b_ids = [
        *await _seed_products(sessionmaker, user_b, 1),
        *await _seed_products(sessionmaker, user_b, 1, Marketplace.ozon),
    ]
    rig = _rig(engine, sessionmaker)

    report = _consistent(await rig.refresher.refresh_list(_TG_A))

    assert report == RefreshReport(total=2, updated=2, failures={})
    assert [_state(await _row(sessionmaker, i)) for i in a_ids] == [_REFRESHED] * 2
    assert [_state(await _row(sessionmaker, i)) for i in b_ids] == [_UNTOUCHED] * 2
    asked = sorted(a for _m, call in rig.tally.calls for a in call)
    b_articles = sorted([(await _row(sessionmaker, i)).article for i in b_ids])
    assert not set(asked) & set(b_articles)


@pytest.mark.asyncio
async def test_refresh_product_refreshes_only_that_product_of_the_caller(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    first, second = await _seed_products(sessionmaker, user_a, 2)
    rig = _rig(engine, sessionmaker)

    result = await rig.refresher.refresh_product(_TG_A, first)

    assert result == RefreshReport(total=1, updated=1, failures={})
    assert _state(await _row(sessionmaker, first)) == _REFRESHED
    assert _state(await _row(sessionmaker, second)) == _UNTOUCHED


@pytest.mark.asyncio
async def test_refresh_list_of_a_user_without_products_is_an_empty_report(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    rig = _rig(engine, sessionmaker)

    report = await rig.refresher.refresh_list(_TG_A)  # the user is created on the way

    assert report == RefreshReport(total=0, updated=0, failures={})
    assert rig.tally.calls == []


# --- AC3: batches and connections ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_pool_probe_sees_a_session_that_is_open(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Positive control for the ``checkedout`` assertions below."""
    async with sessionmaker() as session:
        await session.execute(text("SELECT 1"))
        assert _checked_out(engine) == 1
    assert _checked_out(engine) == 0


@pytest.mark.asyncio
async def test_153_products_go_in_5_batches_and_no_connection_is_held_at_any_fetch(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    await _seed_products(sessionmaker, user_a, 150)
    await _seed_products(sessionmaker, user_a, 3, Marketplace.ozon)
    rig = _rig(engine, sessionmaker, concurrency=1)

    report = _consistent(await rig.refresher.refresh_list(_TG_A))

    wb = sorted(len(a) for m, a in rig.tally.calls if m is Marketplace.wb)
    ozon = sorted(len(a) for m, a in rig.tally.calls if m is Marketplace.ozon)
    assert (wb, ozon) == ([50, 100], [1, 1, 1])
    assert report == RefreshReport(total=153, updated=153, failures={})
    assert rig.tally.checked_out_at_call == [0] * 5  # AC3: none at any fetch
    assert _checked_out(engine) == 0


@pytest.mark.asyncio
async def test_no_connection_is_held_during_the_fetch_of_a_whole_list(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """One batch, so no write of an earlier batch can overlap with its fetch:
    whatever the selection held would show as a checked-out connection."""
    user_a = await _seed_user(sessionmaker, _TG_A)
    await _seed_products(sessionmaker, user_a, 3)
    rig = _rig(engine, sessionmaker)

    await rig.refresher.refresh_list(_TG_A)

    assert len(rig.tally.calls) == 1
    assert rig.tally.checked_out_at_call == [0]


@pytest.mark.asyncio
async def test_no_connection_is_held_during_the_fetch_of_one_card(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    (product_id,) = await _seed_products(sessionmaker, user_a, 1, Marketplace.ozon)
    rig = _rig(engine, sessionmaker)

    await rig.refresher.refresh_product(_TG_A, product_id)

    assert len(rig.tally.calls) == 1
    assert rig.tally.checked_out_at_call == [0]


@pytest.mark.asyncio
async def test_in_flight_fetches_never_exceed_the_concurrency(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    await _seed_products(sessionmaker, user_a, 150)
    await _seed_products(sessionmaker, user_a, 3, Marketplace.ozon)
    rig = _rig(engine, sessionmaker, concurrency=2)

    _consistent(await rig.refresher.refresh_list(_TG_A))

    assert rig.tally.peak == 2  # the overlap is real, and never above the limit
    assert _checked_out(engine) == 0


# --- AC4: a block stops the rest of its marketplace ----------------------------------


@pytest.mark.asyncio
async def test_a_block_leaves_the_unrequested_ozon_products_as_they_were(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    ozon_ids = await _seed_products(sessionmaker, user_a, 3, Marketplace.ozon)
    wb_ids = await _seed_products(sessionmaker, user_a, 2)
    rig = _rig(engine, sessionmaker, concurrency=1)
    ozon_articles = {(await _row(sessionmaker, i)).article for i in ozon_ids}
    rig.tally.answers = {
        a: MarketplaceFetchFailure(reason=FetchFailureReason.blocked, detail="stub")
        for a in ozon_articles
    }

    report = _consistent(await rig.refresher.refresh_list(_TG_A))

    ozon_calls = [a for m, a in rig.tally.calls if m is Marketplace.ozon]
    assert len(ozon_calls) == 1
    assert report == RefreshReport(5, 2, {RefreshFailure.blocked: 3})
    ozon_rows = {i: await _row(sessionmaker, i) for i in ozon_ids}
    asked = ozon_calls[0][0]
    for row in ozon_rows.values():
        if row.article == asked:
            # asked and refused: the check is recorded, the price is not touched
            assert (row.current_price, row.last_checked_at) == (_START_PRICE, _NOW)
            assert row.price_fetched_at == _OLD_STAMP
        else:
            assert _state(row) == _UNTOUCHED
    assert [_state(await _row(sessionmaker, i)) for i in wb_ids] == [_REFRESHED] * 2


# --- AC5: writes, thresholds, cooldown, failures -------------------------------------


@pytest.mark.asyncio
async def test_success_writes_price_previous_price_and_both_timestamps(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    (product_id,) = await _seed_products(sessionmaker, user_a, 1)
    rig = _rig(engine, sessionmaker)

    await rig.refresher.refresh_list(_TG_A)

    row = await _row(sessionmaker, product_id)
    assert (row.current_price, row.previous_price) == (_NEW_PRICE, _START_PRICE)
    assert (row.last_checked_at, row.price_fetched_at) == (_NOW, _NOW)


@pytest.mark.asyncio
async def test_a_failed_fetch_moves_last_checked_at_only(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    (product_id,) = await _seed_products(sessionmaker, user_a, 1)
    rig = _rig(engine, sessionmaker)
    article = (await _row(sessionmaker, product_id)).article
    rig.tally.answers = {
        article: MarketplaceFetchFailure(
            reason=FetchFailureReason.out_of_stock, detail="stub"
        )
    }

    report = await rig.refresher.refresh_list(_TG_A)

    assert report == RefreshReport(1, 0, {RefreshFailure.out_of_stock: 1})
    assert _state(await _row(sessionmaker, product_id)) == (
        _START_PRICE,
        None,
        _NOW,
        _OLD_STAMP,
    )


async def _seed_alert(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: int,
    product_id: int,
    *,
    direction: AlertDirection,
    target: int,
    triggered_at: datetime | None = None,
) -> int:
    async with sessionmaker() as session, session.begin():
        alert = await make_alert(
            session,
            user_id=user_id,
            product_id=product_id,
            direction=direction,
            target_price=target,
        )
        alert.triggered_at = triggered_at
        return alert.id


async def _triggered_at(
    sessionmaker: async_sessionmaker[AsyncSession], alert_id: int
) -> datetime | None:
    async with sessionmaker() as session:
        return (
            await session.execute(
                select(Alert.triggered_at).where(Alert.id == alert_id)
            )
        ).scalar_one()


def _sent(rig: _Rig) -> list[SendMessage]:
    return [r for r in rig.bot_requests if isinstance(r, SendMessage)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "target", "notified"),
    [
        (AlertDirection.below, _NEW_PRICE - 1, False),
        (AlertDirection.below, _NEW_PRICE, True),
        (AlertDirection.below, _NEW_PRICE + 1, True),
        (AlertDirection.above, _NEW_PRICE + 1, False),
        (AlertDirection.above, _NEW_PRICE, True),
        (AlertDirection.above, _NEW_PRICE - 1, True),
    ],
)
async def test_the_refresh_notifies_exactly_when_the_new_price_reaches_the_threshold(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    direction: AlertDirection,
    target: int,
    notified: bool,
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    (product_id,) = await _seed_products(sessionmaker, user_a, 1)
    alert_id = await _seed_alert(
        sessionmaker, user_a, product_id, direction=direction, target=target
    )
    rig = _rig(engine, sessionmaker)

    await rig.refresher.refresh_list(_TG_A)

    assert len(_sent(rig)) == int(notified)
    assert (await _triggered_at(sessionmaker, alert_id) == _NOW) is notified
    if notified:
        assert _sent(rig)[0].chat_id == _TG_A


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age_seconds", "notified"),
    [(_COOLDOWN - 1, False), (_COOLDOWN, True), (_COOLDOWN + 1, True)],
)
async def test_the_cooldown_is_counted_from_triggered_at_to_the_second(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    age_seconds: int,
    notified: bool,
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    (product_id,) = await _seed_products(sessionmaker, user_a, 1)
    previous = _NOW - timedelta(seconds=age_seconds)
    alert_id = await _seed_alert(
        sessionmaker,
        user_a,
        product_id,
        direction=AlertDirection.below,
        target=_NEW_PRICE,
        triggered_at=previous,
    )
    rig = _rig(engine, sessionmaker)

    await rig.refresher.refresh_list(_TG_A)

    assert len(_sent(rig)) == int(notified)
    assert await _triggered_at(sessionmaker, alert_id) == (
        _NOW if notified else previous
    )


@pytest.mark.asyncio
async def test_a_second_refresh_inside_the_cooldown_does_not_notify_again(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    (product_id,) = await _seed_products(sessionmaker, user_a, 1)
    await _seed_alert(
        sessionmaker,
        user_a,
        product_id,
        direction=AlertDirection.below,
        target=_NEW_PRICE,
    )
    rig = _rig(engine, sessionmaker)

    await rig.refresher.refresh_list(_TG_A)
    await rig.refresher.refresh_list(_TG_A)

    assert len(_sent(rig)) == 1


@pytest.mark.asyncio
async def test_a_failed_delivery_does_not_stop_the_other_alerts_or_the_price_writes(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    first, second = await _seed_products(sessionmaker, user_a, 2)
    alerts = [
        await _seed_alert(
            sessionmaker, user_a, p, direction=AlertDirection.below, target=_NEW_PRICE
        )
        for p in (first, second)
    ]
    rig = _rig(engine, sessionmaker, send_failures=1)

    report = _consistent(await rig.refresher.refresh_list(_TG_A))

    assert report == RefreshReport(2, 2, {})
    assert len(_sent(rig)) == 2  # one refused by Telegram, one delivered
    stamps = [await _triggered_at(sessionmaker, a) for a in alerts]
    assert sorted(s is not None for s in stamps) == [False, True]
    assert [(await _row(sessionmaker, p)).current_price for p in (first, second)] == [
        _NEW_PRICE,
        _NEW_PRICE,
    ]


@pytest.mark.asyncio
async def test_a_failing_write_rolls_back_only_its_own_product_and_is_logged(
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    log_records: list[Record],
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    first, failing, third = await _seed_products(sessionmaker, user_a, 3)
    rig = _rig(engine, sessionmaker)
    rig.service.fail_for = failing

    report = _consistent(await rig.refresher.refresh_list(_TG_A))

    assert report == RefreshReport(3, 2, {RefreshFailure.error: 1})
    assert (
        _state(await _row(sessionmaker, failing)) == _UNTOUCHED
    )  # flushed, rolled back
    assert _state(await _row(sessionmaker, first)) == _REFRESHED
    assert _state(await _row(sessionmaker, third)) == _REFRESHED
    errors = [r for r in log_records if r["level"].name == "ERROR"]
    assert [r["extra"]["product_id"] for r in errors] == [failing]
    assert _checked_out(engine) == 0


# --- AC6: the deadline on a real database ---------------------------------------------


@pytest.mark.asyncio
async def test_the_deadline_keeps_what_was_committed_and_frees_every_connection(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    wb_ids = await _seed_products(sessionmaker, user_a, 2)
    ozon_ids = await _seed_products(sessionmaker, user_a, 2, Marketplace.ozon)
    rig = _rig(engine, sessionmaker, deadline=0.2)
    rig.tally.hang_for = {Marketplace.ozon}

    async with asyncio.timeout(_HANG_GUARD):
        report = _consistent(await rig.refresher.refresh_list(_TG_A))

    assert report == RefreshReport(4, 2, {RefreshFailure.timed_out: 2})
    assert [_state(await _row(sessionmaker, i)) for i in wb_ids] == [_REFRESHED] * 2
    assert [_state(await _row(sessionmaker, i)) for i in ozon_ids] == [_UNTOUCHED] * 2
    assert _checked_out(engine) == 0


@pytest.mark.asyncio
async def test_an_external_cancel_frees_every_connection(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    await _seed_products(sessionmaker, user_a, 2, Marketplace.ozon)
    rig = _rig(engine, sessionmaker)
    rig.tally.hang_for = {Marketplace.ozon}
    task = asyncio.create_task(rig.refresher.refresh_list(_TG_A))
    async with asyncio.timeout(_HANG_GUARD):
        await rig.tally.first_call.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert _checked_out(engine) == 0


def _past_short_deadline() -> float:
    """Loop time a little after the refresher's deadline (taken at its start)."""
    return asyncio.get_running_loop().time() + _SHORT_DEADLINE + 0.05


@pytest.mark.asyncio
async def test_a_write_the_deadline_hits_after_the_notification_stays_committed(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    (product_id,) = await _seed_products(sessionmaker, user_a, 1)
    alert_id = await _seed_alert(
        sessionmaker,
        user_a,
        product_id,
        direction=AlertDirection.below,
        target=_NEW_PRICE,
    )
    rig = _rig(engine, sessionmaker, deadline=_SHORT_DEADLINE)
    rig.service.stall_past = _past_short_deadline()

    async with asyncio.timeout(_HANG_GUARD):
        report = _consistent(await rig.refresher.refresh_list(_TG_A))

    assert report == RefreshReport(1, 1, {})
    assert len(_sent(rig)) == 1
    assert await _triggered_at(sessionmaker, alert_id) == _NOW  # not rolled back
    assert _state(await _row(sessionmaker, product_id)) == _REFRESHED
    assert _checked_out(engine) == 0


@pytest.mark.asyncio
async def test_no_write_starts_after_the_deadline_so_the_rest_stays_untouched(
    engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user_a = await _seed_user(sessionmaker, _TG_A)
    ids = await _seed_products(sessionmaker, user_a, 3)
    alerts = [
        await _seed_alert(
            sessionmaker, user_a, p, direction=AlertDirection.below, target=_NEW_PRICE
        )
        for p in ids
    ]
    rig = _rig(engine, sessionmaker, deadline=_SHORT_DEADLINE)
    rig.service.stall_past = _past_short_deadline()

    async with asyncio.timeout(_HANG_GUARD):
        report = _consistent(await rig.refresher.refresh_list(_TG_A))

    assert report == RefreshReport(3, 1, {RefreshFailure.timed_out: 2})
    states = [_state(await _row(sessionmaker, p)) for p in ids]
    assert sorted(states, key=lambda st: st != _REFRESHED) == [
        _REFRESHED,
        _UNTOUCHED,
        _UNTOUCHED,
    ]
    stamps = [await _triggered_at(sessionmaker, a) for a in alerts]
    assert [s is not None for s in stamps] == [st == _REFRESHED for st in states]
    assert len(_sent(rig)) == 1
    assert _checked_out(engine) == 0
