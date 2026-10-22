"""Paging queries of ``ProductRepository`` on a real PostgreSQL (PAB-108).

``count_by_user``, ``get_page_by_user`` and ``count_before`` carry the list
paging; ownership scoping and ordering are properties of the SQL, so they are
proven here and not on mocks. Isolation: the ``db_session`` fixture rolls its
transaction back after every test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.repositories.product import ProductRepository
from tests.integration.repositories.conftest import make_product
from tests.integration.repositories.conftest import make_user

pytestmark = pytest.mark.asyncio


async def _seed(session: AsyncSession, user_id: int, count: int) -> list[int]:
    return [(await make_product(session, user_id=user_id)).id for _ in range(count)]


async def test_get_page_by_user_splits_23_products_into_10_10_3_by_id(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    ids = await _seed(db_session, user.id, 23)

    pages = [
        [p.id for p in await repository.get_page_by_user(user.id, 10, offset)]
        for offset in (0, 10, 20)
    ]

    assert pages == [ids[:10], ids[10:20], ids[20:]]


async def test_get_page_by_user_orders_by_id_not_by_article_or_physical_position(
    db_session: AsyncSession,
) -> None:
    """Articles descend while ids ascend and two rows are rewritten (an UPDATE
    moves a row's physical position), so any order but ``id`` shows up."""
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    ids = [
        (await make_product(db_session, user_id=user.id, article=9_000_000 - n)).id
        for n in range(6)
    ]
    # ``article`` is part of a unique index, so this is not a HOT update: the
    # row gets a new physical position *and* a new index entry after the rest.
    for product_id, article in ((ids[0], 5_000_001), (ids[1], 5_000_002)):
        await db_session.execute(
            update(Product).where(Product.id == product_id).values(article=article)
        )

    page = await repository.get_page_by_user(user.id, 10, 0)

    assert [p.id for p in page] == ids


async def test_get_page_by_user_returns_only_the_users_products(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    owner = await make_user(db_session)
    other = await make_user(db_session)
    own = await _seed(db_session, owner.id, 3)
    await _seed(db_session, other.id, 15)

    page = await repository.get_page_by_user(owner.id, 10, 0)

    assert [p.id for p in page] == own
    assert {p.user_id for p in page} == {owner.id}


async def test_get_page_by_user_with_an_offset_past_the_end_is_empty(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    await _seed(db_session, user.id, 23)

    assert await repository.get_page_by_user(user.id, 10, 23) == []
    assert await repository.get_page_by_user(user.id, 10, 1_000_000) == []


async def test_get_page_by_user_respects_the_limit_with_a_partial_offset(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    ids = await _seed(db_session, user.id, 12)

    page = await repository.get_page_by_user(user.id, 3, 5)

    assert [p.id for p in page] == ids[5:8]


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (-1, 0), (10, -1)])
async def test_get_page_by_user_rejects_a_bad_limit_or_offset(
    db_session: AsyncSession, limit: int, offset: int
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    await _seed(db_session, user.id, 2)

    with pytest.raises(ValueError, match="limit|offset"):
        await repository.get_page_by_user(user.id, limit, offset)


async def test_count_by_user_counts_only_the_users_products(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    owner = await make_user(db_session)
    other = await make_user(db_session)
    empty = await make_user(db_session)
    await _seed(db_session, owner.id, 23)
    await _seed(db_session, other.id, 4)

    assert await repository.count_by_user(owner.id) == 23
    assert await repository.count_by_user(other.id) == 4
    assert await repository.count_by_user(empty.id) == 0


@pytest.mark.parametrize(("position", "before"), [(0, 0), (10, 10), (22, 22)])
async def test_count_before_is_the_zero_based_position_among_the_users_products(
    db_session: AsyncSession, position: int, before: int
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    other = await make_user(db_session)
    ids = await _seed(db_session, user.id, 23)
    await _seed(db_session, other.id, 5)  # ids interleaved after: must not count

    assert await repository.count_before(user.id, ids[position]) == before


async def test_count_before_ignores_foreign_products_with_lower_ids(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    other = await make_user(db_session)
    owner = await make_user(db_session)
    await _seed(db_session, other.id, 7)  # lower ids than the owner's
    owner_ids = await _seed(db_session, owner.id, 3)

    assert await repository.count_before(owner.id, owner_ids[2]) == 2


async def test_count_before_of_a_deleted_product_counts_the_survivors_below_it(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    ids = await _seed(db_session, user.id, 5)
    assert await repository.delete_for_user(ids[3], user.id) is True

    assert await repository.count_before(user.id, ids[3]) == 3


async def test_count_before_of_a_foreign_product_id_counts_only_the_callers_products(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    owner = await make_user(db_session)
    other = await make_user(db_session)
    own = await _seed(db_session, owner.id, 2)
    foreign = await _seed(db_session, other.id, 30)

    assert await repository.count_before(owner.id, foreign[29]) == len(own)
