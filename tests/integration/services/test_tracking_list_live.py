"""List, card and removal of TrackingService on a real PostgreSQL (PAB-070).

What only a real database can show: the ownership predicates, the DB-level
``ON DELETE CASCADE`` against ORM identity-map state, the unique constraint
after a removal, and the number of statements (convention 25: counted with
``before_cursor_execute`` on the real ``Engine``). Isolation comes from the
``db_session`` fixture (outer transaction, rolled back after each test).
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.repositories.product import ProductRepository
from app.services.base_client import BaseMarketplaceClient
from app.services.tracking import AlertRemoval
from app.services.tracking import TrackingService

pytestmark = pytest.mark.asyncio

_WB = Marketplace.wb
_tg_ids = itertools.count(710_000_001)
_articles = itertools.count(6_000_001)
_PRICE = 15_000


def _service(session: AsyncSession) -> TrackingService:
    return TrackingService(
        session=session,
        http_client=httpx.AsyncClient(),
        client_factory=lambda _m, _h: _NoFetchClient(),
        max_products_per_user=100,
    )


class _NoFetchClient(BaseMarketplaceClient):
    async def get_product_data(self, article: int) -> Any:
        raise AssertionError("list/card/removal must not call the marketplace")


async def _add(
    service: TrackingService,
    tg: int,
    *,
    article: int | None = None,
    target: int = 9_000,
    name: str = "Live item",
) -> Alert:
    return await service.add_tracking(
        tg,
        _WB,
        article if article is not None else next(_articles),
        name,
        _PRICE,
        target,
        price_fetched_at=None,
    )


async def _count(session: AsyncSession, model: type[Product] | type[Alert]) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _count_statements(
    session: AsyncSession, action: Callable[[], Awaitable[object]]
) -> tuple[int, int]:
    """Return (cursor executes, executemany executes) issued by ``action``."""
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
    return len(calls), sum(calls)


# --- list_page / list_page_of (PAB-108) -----------------------------------------


async def _seed(service: TrackingService, tg: int, count: int) -> list[int]:
    """Add ``count`` products for ``tg``; return their ids ascending."""
    return [(await _add(service, tg)).product_id for _ in range(count)]


async def test_list_page_splits_23_products_into_10_10_3_by_ascending_id(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    ids = await _seed(service, tg, 23)

    pages = [await service.list_page(tg, n) for n in (1, 2, 3)]

    assert [[p.id for p in page.products] for page in pages] == [
        ids[:10],
        ids[10:20],
        ids[20:],
    ]
    assert [(p.page, p.pages, p.total) for p in pages] == [
        (1, 3, 23),
        (2, 3, 23),
        (3, 3, 23),
    ]


async def test_list_page_never_shows_a_foreign_product_on_any_page(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    owner, other = next(_tg_ids), next(_tg_ids)
    own = await _seed(service, owner, 12)
    foreign = await _seed(service, other, 12)

    for number in (0, 1, 2, 3, 99):
        for tg, mine in ((owner, own), (other, foreign)):
            page = await service.list_page(tg, number)
            assert {p.id for p in page.products} <= set(mine)
            assert page.total == 12
    owner_page = await service.list_page(owner, 2)
    assert [p.id for p in owner_page.products] == own[10:]


@pytest.mark.parametrize(("asked", "shown"), [(0, 1), (-5, 1), (99, 3), (3, 3)])
async def test_list_page_clamps_the_page_number_with_23_products(
    db_session: AsyncSession, asked: int, shown: int
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    ids = await _seed(service, tg, 23)

    page = await service.list_page(tg, asked)

    assert page.page == shown
    assert [p.id for p in page.products] == ids[(shown - 1) * 10 : shown * 10]


@pytest.mark.parametrize(("total", "pages"), [(10, 1), (11, 2)])
async def test_list_page_counts_ten_products_as_one_page_and_eleven_as_two(
    db_session: AsyncSession, total: int, pages: int
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    await _seed(service, tg, total)

    page = await service.list_page(tg, 1)

    assert (page.total, page.pages, len(page.products)) == (total, pages, 10)


async def test_list_page_of_a_user_without_products_is_one_empty_page(
    db_session: AsyncSession,
) -> None:
    page = await _service(db_session).list_page(next(_tg_ids), 7)

    assert (page.products, page.total, page.page, page.pages) == ((), 0, 1, 1)


@pytest.mark.parametrize(
    ("position", "page"), [(1, 1), (10, 1), (11, 2), (15, 2), (23, 3)]
)
async def test_list_page_of_gives_the_page_holding_the_nth_product(
    db_session: AsyncSession, position: int, page: int
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    ids = await _seed(service, tg, 23)

    assert await service.list_page_of(tg, ids[position - 1]) == page


async def test_list_page_of_a_deleted_product_is_the_page_it_was_on(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    ids = await _seed(service, tg, 23)
    assert await service.remove_product(tg, ids[14])

    assert await service.list_page_of(tg, ids[14]) == 2


async def test_list_page_of_the_only_product_of_page_three_removed_is_page_two(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    ids = await _seed(service, tg, 21)
    assert await service.remove_product(tg, ids[20])

    assert await service.list_page_of(tg, ids[20]) == 2


async def test_list_page_of_a_foreign_product_reveals_nothing_about_it(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    owner, other = next(_tg_ids), next(_tg_ids)
    await _seed(service, owner, 3)
    foreign = await _seed(service, other, 25)

    assert await service.list_page_of(owner, foreign[24]) == 1


async def test_list_page_statement_count_is_the_same_for_3_and_25_products(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    small, big = next(_tg_ids), next(_tg_ids)
    await _seed(service, small, 3)
    await _seed(service, big, 25)

    small_count = await _count_statements(
        db_session, lambda: service.list_page(small, 1)
    )
    big_count = await _count_statements(db_session, lambda: service.list_page(big, 1))

    # INSERT .. ON CONFLICT + SELECT of the existing user (get_or_create), then
    # COUNT and the page: four, whatever N is. The ticket counted the user as one.
    assert small_count == (4, 0)
    assert big_count == (4, 0)


async def test_card_costs_exactly_one_statement_more_than_without_the_page_lookup(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    ids = await _seed(service, tg, 25)

    with_lookup = await _count_statements(
        db_session, lambda: service.get_product_card(tg, ids[20])
    )

    async def no_lookup(_self: ProductRepository, _user: int, _product: int) -> int:
        return 0

    monkeypatch.setattr(ProductRepository, "count_before", no_lookup)
    baseline = await _count_statements(
        db_session, lambda: service.get_product_card(tg, ids[20])
    )

    assert baseline[0] >= 3  # positive control: user, product, alerts
    assert with_lookup == (baseline[0] + 1, 0)


async def test_card_list_page_is_the_page_of_the_product_for_both_entries(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    ids = await _seed(service, tg, 23)
    products = (await service.list_page(tg, 2)).products
    fifteenth = ids[14]
    assert fifteenth in [p.id for p in products]

    by_id = await service.get_product_card(tg, fifteenth)
    by_article = await service.find_tracked_product(
        tg, _WB, next(p.article for p in products if p.id == fifteenth)
    )

    assert by_id is not None and by_id.list_page == 2
    assert by_article is not None and by_article.list_page == 2
    first = await service.get_product_card(tg, ids[0])
    last = await service.get_product_card(tg, ids[22])
    assert first is not None and first.list_page == 1
    assert last is not None and last.list_page == 3


# --- get_product_card -----------------------------------------------------------


async def test_card_has_the_product_and_alerts_sorted_by_target_price(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg, article = next(_tg_ids), next(_articles)
    for target in (20_000, 3_000, 9_000):
        await _add(service, tg, article=article, target=target)
    product_id = (await service.list_page(tg, 1)).products[0].id

    card = await service.get_product_card(tg, product_id)

    assert card is not None
    assert card.product.id == product_id
    assert [a.target_price for a in card.alerts] == [3_000, 9_000, 20_000]
    assert [a.direction for a in card.alerts] == [
        AlertDirection.below,
        AlertDirection.below,
        AlertDirection.above,
    ]


async def test_card_statement_count_is_the_same_for_1_and_30_alerts(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    one_article, many_article = next(_articles), next(_articles)
    await _add(service, tg, article=one_article, target=1_000)
    for target in range(1_000, 1_030):
        await _add(service, tg, article=many_article, target=target)
    by_article = {p.article: p.id for p in (await service.list_page(tg, 1)).products}

    one = await _count_statements(
        db_session, lambda: service.get_product_card(tg, by_article[one_article])
    )
    many = await _count_statements(
        db_session, lambda: service.get_product_card(tg, by_article[many_article])
    )

    assert one[0] >= 1
    assert many == one
    card = await service.get_product_card(tg, by_article[many_article])
    assert card is not None
    assert len(card.alerts) == 30


async def test_card_of_a_foreign_or_missing_product_is_none(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    owner, intruder = next(_tg_ids), next(_tg_ids)
    alert = await _add(service, owner)

    assert await service.get_product_card(intruder, alert.product_id) is None
    assert await service.get_product_card(owner, 2_000_000_000) is None
    assert await service.get_product_card(owner, alert.product_id) is not None


# --- ownership (AC3) --------------------------------------------------------------


async def test_foreign_ids_are_refused_and_the_owners_rows_survive(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    owner, intruder = next(_tg_ids), next(_tg_ids)
    alert = await _add(service, owner)
    await service.list_page(intruder, 1)  # the intruder exists as a user too
    products_before = await _count(db_session, Product)
    alerts_before = await _count(db_session, Alert)

    assert await service.remove_product(intruder, alert.product_id) is False
    assert await service.remove_alert(intruder, alert.id) is AlertRemoval.not_found

    assert await _count(db_session, Product) == products_before
    assert await _count(db_session, Alert) == alerts_before
    stored = (
        await db_session.execute(select(Alert).where(Alert.id == alert.id))
    ).scalar_one()
    assert (stored.user_id, stored.product_id) == (alert.user_id, alert.product_id)
    assert await service.get_product_card(owner, alert.product_id) is not None


async def test_missing_ids_give_the_same_refusals_as_foreign_ones(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    await _add(service, tg)

    assert await service.remove_product(tg, 2_000_000_000) is False
    assert await service.remove_alert(tg, 2_000_000_000) is AlertRemoval.not_found


# --- remove_product (AC4) -----------------------------------------------------------


async def test_remove_product_after_card_flushes_cleanly_and_leaves_no_rows(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg, article = next(_tg_ids), next(_articles)
    await _add(service, tg, article=article, target=9_000)
    await _add(service, tg, article=article, target=8_000)
    product_id = (await service.list_page(tg, 1)).products[0].id
    card = await service.get_product_card(tg, product_id)
    assert card is not None
    assert len(card.alerts) == 2  # alerts are loaded into the session

    assert await service.remove_product(tg, product_id) is True
    await db_session.flush()  # StaleDataError here would abort the transaction

    assert (await db_session.execute(select(1))).scalar_one() == 1  # still usable
    assert (
        await db_session.execute(select(Product).where(Product.id == product_id))
    ).first() is None
    assert (
        await db_session.execute(select(Alert).where(Alert.product_id == product_id))
    ).first() is None


async def test_remove_product_keeps_the_other_products_of_the_user(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    gone = await _add(service, tg)
    kept = await _add(service, tg)

    assert await service.remove_product(tg, gone.product_id) is True

    assert [p.id for p in (await service.list_page(tg, 1)).products] == [
        kept.product_id
    ]
    assert await _count(db_session, Alert) == 1


# --- remove_alert (AC5, AC6) ----------------------------------------------------------


async def test_remove_alert_with_siblings_keeps_the_product_and_other_alerts(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg, article = next(_tg_ids), next(_articles)
    first = await _add(service, tg, article=article, target=9_000)
    second = await _add(service, tg, article=article, target=8_000)

    assert await service.remove_alert(tg, first.id) is AlertRemoval.removed

    card = await service.get_product_card(tg, first.product_id)
    assert card is not None
    assert [a.id for a in card.alerts] == [second.id]


async def test_remove_last_alert_drops_the_product_and_its_rows(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    alert = await _add(service, tg)

    assert await service.remove_alert(tg, alert.id) is AlertRemoval.removed_with_product

    assert await service.get_product_card(tg, alert.product_id) is None
    assert (
        await db_session.execute(select(Product).where(Product.id == alert.product_id))
    ).first() is None
    assert (
        await db_session.execute(select(Alert).where(Alert.id == alert.id))
    ).first() is None
    await db_session.flush()  # the session is consistent


async def test_remove_last_alert_does_not_touch_other_products(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg = next(_tg_ids)
    gone = await _add(service, tg)
    kept = await _add(service, tg)

    await service.remove_alert(tg, gone.id)

    assert [p.id for p in (await service.list_page(tg, 1)).products] == [
        kept.product_id
    ]


async def test_remove_alert_twice_is_removed_then_not_found(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    tg, article = next(_tg_ids), next(_articles)
    first = await _add(service, tg, article=article, target=9_000)
    sibling = await _add(service, tg, article=article, target=8_000)
    foreign = await _add(service, next(_tg_ids), article=article, target=9_000)

    assert await service.remove_alert(tg, first.id) is AlertRemoval.removed
    assert await service.remove_alert(tg, first.id) is AlertRemoval.not_found

    surviving = (
        (
            await db_session.execute(
                select(Alert.id).where(Alert.id.in_([first.id, sibling.id, foreign.id]))
            )
        )
        .scalars()
        .all()
    )
    assert sorted(surviving) == sorted([sibling.id, foreign.id])


@pytest.mark.parametrize("keep_sibling", [True, False])
async def test_same_threshold_can_be_added_again_after_removal(
    db_session: AsyncSession, keep_sibling: bool
) -> None:
    """No ``DuplicateAlertError`` and no leftover ``is_active = false`` row."""
    service = _service(db_session)
    tg, article = next(_tg_ids), next(_articles)
    removed = await _add(service, tg, article=article, target=9_000)
    if keep_sibling:
        await _add(service, tg, article=article, target=8_000)
    await service.remove_alert(tg, removed.id)

    again = await _add(service, tg, article=article, target=9_000)

    assert again.is_active is True
    assert again.direction is AlertDirection.below
    assert again.target_price == 9_000
    user_id = (
        await db_session.execute(select(User.id).where(User.tg_user_id == tg))
    ).scalar_one()
    inactive = await db_session.execute(
        select(func.count())
        .select_from(Alert)
        .where(Alert.user_id == user_id, Alert.is_active.is_(False))
    )
    assert inactive.scalar_one() == 0
