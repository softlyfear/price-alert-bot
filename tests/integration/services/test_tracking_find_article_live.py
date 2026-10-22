"""``TrackingService.find_tracked_article`` on a real PostgreSQL (PAB-113, R21).

What only a real database can show: which marketplace wins when a bare article
is tracked on one or both, the owner predicate, and the statement count
against ``find_tracked_product`` (convention 25: ``before_cursor_execute`` on
the real ``Engine``). Isolation comes from the ``db_session`` fixture (outer
transaction, rolled back after each test).
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

from app.models.enums import Marketplace
from app.services.base_client import BaseMarketplaceClient
from app.services.tracking import TrackingService

pytestmark = pytest.mark.asyncio

_tg_ids = itertools.count(730_000_001)
_articles = itertools.count(8_000_001)
_PRICE = 15_000


def _service(
    session: AsyncSession, factory_calls: list[Marketplace]
) -> TrackingService:
    def factory(
        marketplace: Marketplace, http: httpx.AsyncClient
    ) -> BaseMarketplaceClient:
        factory_calls.append(marketplace)
        raise AssertionError("the lookup must not build a marketplace client")

    return TrackingService(
        session=session,
        http_client=httpx.AsyncClient(),
        client_factory=factory,
        max_products_per_user=100,
    )


async def _track(
    service: TrackingService, tg: int, article: int, marketplace: Marketplace
) -> None:
    await service.add_tracking(
        tg, marketplace, article, f"{marketplace} item", _PRICE, 9_000,
        price_fetched_at=None,
    )  # fmt: skip


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


async def test_only_wildberries_tracked_gives_the_wildberries_card(
    db_session: AsyncSession,
) -> None:
    factory_calls: list[Marketplace] = []
    service = _service(db_session, factory_calls)
    tg, article = next(_tg_ids), next(_articles)
    await _track(service, tg, article, Marketplace.wb)

    card = await service.find_tracked_article(tg, article)

    assert card is not None
    assert (card.product.marketplace, card.product.article) == (Marketplace.wb, article)
    assert factory_calls == []


async def test_only_ozon_tracked_gives_the_ozon_card(db_session: AsyncSession) -> None:
    factory_calls: list[Marketplace] = []
    service = _service(db_session, factory_calls)
    tg, article = next(_tg_ids), next(_articles)
    await _track(service, tg, article, Marketplace.ozon)

    card = await service.find_tracked_article(tg, article)

    assert card is not None
    assert (card.product.marketplace, card.product.article) == (
        Marketplace.ozon,
        article,
    )
    assert factory_calls == []


async def test_tracked_on_both_marketplaces_wildberries_wins(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session, [])
    tg, article = next(_tg_ids), next(_articles)
    await _track(service, tg, article, Marketplace.ozon)
    await _track(service, tg, article, Marketplace.wb)

    card = await service.find_tracked_article(tg, article)

    assert card is not None
    assert card.product.marketplace is Marketplace.wb


@pytest.mark.parametrize("marketplace", list(Marketplace))
async def test_another_users_product_and_an_unknown_article_are_none(
    db_session: AsyncSession, marketplace: Marketplace
) -> None:
    service = _service(db_session, [])
    owner, intruder = next(_tg_ids), next(_tg_ids)
    article = next(_articles)
    await _track(service, owner, article, marketplace)

    assert await service.find_tracked_article(intruder, article) is None
    assert await service.find_tracked_article(owner, next(_articles)) is None
    # Positive control: the owner does see the product.
    assert await service.find_tracked_article(owner, article) is not None


async def test_a_miss_costs_exactly_one_statement_more_than_find_tracked_product(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session, [])
    tg = next(_tg_ids)
    await _track(service, tg, next(_articles), Marketplace.wb)  # user exists
    missing = next(_articles)

    single = await _count_statements(
        db_session, lambda: service.find_tracked_product(tg, Marketplace.wb, missing)
    )
    both = await _count_statements(
        db_session, lambda: service.find_tracked_article(tg, missing)
    )

    assert single >= 1  # positive control: the counter does observe SQL
    assert both == single + 1


async def test_a_wildberries_hit_costs_as_many_statements_as_find_tracked_product(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session, [])
    tg, article = next(_tg_ids), next(_articles)
    await _track(service, tg, article, Marketplace.wb)

    single = await _count_statements(
        db_session, lambda: service.find_tracked_product(tg, Marketplace.wb, article)
    )
    bare = await _count_statements(
        db_session, lambda: service.find_tracked_article(tg, article)
    )

    assert single >= 1
    assert bare == single
