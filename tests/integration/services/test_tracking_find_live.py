"""``TrackingService.find_tracked_product`` on a real PostgreSQL (PAB-096).

What only a real database can show: the marketplace and owner predicates of
the lookup, the sort order of the card, and a constant number of statements
(convention 25: counted with ``before_cursor_execute`` on the real ``Engine``).
Isolation comes from the ``db_session`` fixture (outer transaction, rolled back
after each test).
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.services.base_client import BaseMarketplaceClient
from app.services.tracking import TrackingService

pytestmark = pytest.mark.asyncio

_tg_ids = itertools.count(720_000_001)
_articles = itertools.count(7_000_001)
_PRICE = 15_000


class _NoFetchClient(BaseMarketplaceClient):
    async def get_product_data(self, article: int) -> Any:
        raise AssertionError("the lookup must not call the marketplace")


def _service(session: AsyncSession, factory_calls: list[object]) -> TrackingService:
    def factory(marketplace: Marketplace, http: httpx.AsyncClient) -> _NoFetchClient:
        factory_calls.append(marketplace)
        return _NoFetchClient()

    return TrackingService(
        session=session,
        http_client=httpx.AsyncClient(),
        client_factory=factory,
        max_products_per_user=100,
    )


async def _add(
    service: TrackingService,
    tg: int,
    article: int,
    target: int,
    marketplace: Marketplace = Marketplace.wb,
) -> Alert:
    return await service.add_tracking(
        tg, marketplace, article, "Live item", _PRICE, target
    )


async def _count_statements(
    session: AsyncSession, action: Callable[[], Awaitable[object]]
) -> int:
    calls: list[bool] = []

    def record(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any,
        executemany: bool,
    ) -> None:  # fmt: skip
        calls.append(executemany)

    engine = session.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(calls)


async def test_own_product_is_found_with_alerts_in_ascending_target_order(
    db_session: AsyncSession,
) -> None:
    factory_calls: list[object] = []
    service = _service(db_session, factory_calls)
    tg, article = next(_tg_ids), next(_articles)
    for target in (20_000, 3_000, 9_000):
        await _add(service, tg, article, target)
    factory_calls.clear()

    card = await service.find_tracked_product(tg, Marketplace.wb, article)

    assert card is not None
    assert (card.product.marketplace, card.product.article) == (Marketplace.wb, article)
    assert [a.target_price for a in card.alerts] == [3_000, 9_000, 20_000]
    assert [a.direction for a in card.alerts] == [
        AlertDirection.below,
        AlertDirection.below,
        AlertDirection.above,
    ]
    assert factory_calls == []


async def test_same_article_on_another_marketplace_is_none(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session, [])
    tg, article = next(_tg_ids), next(_articles)
    await _add(service, tg, article, 9_000)

    assert await service.find_tracked_product(tg, Marketplace.ozon, article) is None


async def test_each_marketplace_finds_its_own_product_for_the_same_article(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session, [])
    tg, article = next(_tg_ids), next(_articles)
    await _add(service, tg, article, 9_000, Marketplace.wb)
    await _add(service, tg, article, 8_000, Marketplace.ozon)

    wb = await service.find_tracked_product(tg, Marketplace.wb, article)
    ozon = await service.find_tracked_product(tg, Marketplace.ozon, article)

    assert wb is not None
    assert ozon is not None
    assert wb.product.id != ozon.product.id
    assert [a.target_price for a in wb.alerts] == [9_000]
    assert [a.target_price for a in ozon.alerts] == [8_000]


async def test_foreign_and_missing_products_are_none(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session, [])
    owner, intruder = next(_tg_ids), next(_tg_ids)
    article = next(_articles)
    await _add(service, owner, article, 9_000)

    assert await service.find_tracked_product(intruder, Marketplace.wb, article) is None
    assert (
        await service.find_tracked_product(owner, Marketplace.wb, next(_articles))
        is None
    )
    assert (
        await service.find_tracked_product(owner, Marketplace.wb, article) is not None
    )


async def test_find_statement_count_is_constant_for_1_and_30_alerts(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session, [])
    tg = next(_tg_ids)
    one_article, many_article = next(_articles), next(_articles)
    await _add(service, tg, one_article, 1_000)
    for target in range(1_000, 1_030):
        await _add(service, tg, many_article, target)

    one = await _count_statements(
        db_session,
        lambda: service.find_tracked_product(tg, Marketplace.wb, one_article),
    )
    many = await _count_statements(
        db_session,
        lambda: service.find_tracked_product(tg, Marketplace.wb, many_article),
    )

    assert one >= 1  # positive control: the counter does observe SQL
    assert many == one
    card = await service.find_tracked_product(tg, Marketplace.wb, many_article)
    assert card is not None
    assert len(card.alerts) == 30
