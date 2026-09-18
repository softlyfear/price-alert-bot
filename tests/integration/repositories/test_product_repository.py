"""Integration tests for app.repositories.product against a live PostgreSQL instance.

Covers ``ProductRepository``'s own query methods, the ownership-scoped
methods added by PAB-010 (PAB-058 Description item 2: three cells each --
own row, another user's row, a missing id, with the first two required to
be observably indistinguishable, PROJECT.md section 8.2), and the two
conditions that are only provable on a live database:

- (з): the named cascade limit on ``delete_for_user`` -- the DB-level
  ``ON DELETE CASCADE`` removes dependent ``alerts`` rows, but an
  ``Alert`` already loaded into *this* session before the delete is not
  evicted, and mutating it afterwards raises ``StaleDataError``.
- (и): ``delete_for_user`` itself evicts the deleted ``Product`` from the
  session's identity map -- shown as distinct from both a tautology (a
  bare ``SELECT``, or ``session.get()`` alone, would give ``None`` no
  matter what synchronization strategy was used) and from mere expiry
  (identity-map membership plus a successful, non-lazy attribute read are
  what a strategy that only expired the object, rather than evicting it,
  would fail on -- see ``test_delete_for_user_evicts_...`` below), and
  contrasted with a hand-built statement using
  ``synchronize_session=False`` on a second row within the same test
  (convention 22's positive-control requirement, applied to an absence
  claim).
- (л): the docstring's "without an extra round trip" claim, measured by
  counting ``before_cursor_execute`` events on the real ``Engine`` behind
  this fixture's session (convention 25) rather than assumed. Measured
  and reported honestly, not as directed: a regression to
  ``synchronize_session="fetch"`` turns out to be indistinguishable from
  the real ``evaluate`` code by *any* means this suite can observe on
  this dialect -- both leave the identity map in the same state (both
  evict, condition (и) again) *and* both cost exactly one round trip,
  because PostgreSQL's ``RETURNING`` support lets ``fetch`` append
  ``RETURNING products.id`` to the same ``DELETE`` instead of issuing a
  separate ``SELECT`` first. The round-trip count instead guards the
  claim's *other* implicit contrast -- a naive load-then-delete
  implementation, genuinely three round trips here because of
  ``Product.alerts``' cascade -- and the compiled statement text is
  checked separately for the one textual trace ``fetch`` does leave
  (an appended ``RETURNING`` clause), which is what actually catches an
  ``evaluate`` -> ``fetch`` regression. See
  ``test_delete_for_user_issues_exactly_one_round_trip``'s docstring for
  the measurements behind both halves of this paragraph.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.domain.exceptions import InvalidPaginationError
from app.models.enums import Marketplace
from app.models.product import Product
from app.repositories.product import ProductRepository
from tests.integration.repositories.conftest import make_alert
from tests.integration.repositories.conftest import make_product
from tests.integration.repositories.conftest import make_user


@pytest.mark.asyncio
async def test_get_by_user_id_returns_only_that_users_products(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    owner = await make_user(db_session)
    other = await make_user(db_session)
    own_product_1 = await make_product(db_session, user_id=owner.id)
    own_product_2 = await make_product(db_session, user_id=owner.id)
    await make_product(db_session, user_id=other.id)

    result = await repository.get_by_user_id(owner.id)

    assert {p.id for p in result} == {own_product_1.id, own_product_2.id}


@pytest.mark.asyncio
async def test_get_by_article_and_user_finds_the_exact_match(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(
        db_session, user_id=user.id, marketplace=Marketplace.wb, article=42
    )

    found = await repository.get_by_article_and_user(42, user.id, Marketplace.wb)

    assert found is not None
    assert found.id == product.id


@pytest.mark.asyncio
async def test_get_by_article_and_user_returns_none_on_marketplace_mismatch(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    await make_product(
        db_session, user_id=user.id, marketplace=Marketplace.wb, article=43
    )

    found = await repository.get_by_article_and_user(43, user.id, Marketplace.ozon)

    assert found is None


@pytest.mark.asyncio
async def test_count_active_by_user_counts_products_with_an_active_alert(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    with_active_alert = await make_product(db_session, user_id=user.id)
    await make_alert(db_session, user_id=user.id, product_id=with_active_alert.id)
    await make_product(db_session, user_id=user.id)  # no alert at all

    count = await repository.count_active_by_user(user.id)

    assert count == 1


@pytest.mark.asyncio
async def test_count_active_by_user_excludes_products_whose_alerts_are_all_inactive(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    inactive_alert = await make_alert(
        db_session, user_id=user.id, product_id=product.id
    )
    inactive_alert.is_active = False
    await db_session.flush()

    count = await repository.count_active_by_user(user.id)

    assert count == 0


@pytest.mark.asyncio
async def test_get_all_with_active_alerts_returns_distinct_products(
    db_session: AsyncSession,
) -> None:
    """Two active alerts on the same product must not duplicate it in the
    result -- the method's own ``.distinct()`` is what this test would
    catch a regression in.
    """
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    await make_alert(
        db_session, user_id=user.id, product_id=product.id, target_price=100
    )
    await make_alert(
        db_session, user_id=user.id, product_id=product.id, target_price=200
    )
    inactive_product = await make_product(db_session, user_id=user.id)
    inactive_alert = await make_alert(
        db_session, user_id=user.id, product_id=inactive_product.id
    )
    inactive_alert.is_active = False
    await db_session.flush()

    result = await repository.get_all_with_active_alerts()

    matching = [p for p in result if p.id in {product.id, inactive_product.id}]
    assert [p.id for p in matching] == [product.id]


@pytest.mark.asyncio
async def test_patch_updates_only_the_given_field_and_persists_it(
    db_session: AsyncSession,
) -> None:
    """Exercises ``_patch_fields`` (four fields) while only supplying one of
    them -- see the equivalent test on ``AlertRepository`` for why this
    also covers the base class's partial-patch branch.
    """
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id, product_name="old name")

    patched = await repository.patch(product.id, {"product_name": "new name"})

    assert patched is not None
    assert patched.product_name == "new name"
    assert patched.current_price == product.current_price  # untouched field kept


@pytest.mark.asyncio
async def test_get_by_id_for_user_returns_the_owners_product(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)

    found = await repository.get_by_id_for_user(product.id, user.id)

    assert found is not None
    assert found.id == product.id


@pytest.mark.asyncio
async def test_get_by_id_for_user_hides_another_users_product(
    db_session: AsyncSession,
) -> None:
    """PROJECT.md section 8.2: another user's row must be indistinguishable
    from a row that does not exist at all.
    """
    repository = ProductRepository(db_session)
    owner = await make_user(db_session)
    intruder = await make_user(db_session)
    product = await make_product(db_session, user_id=owner.id)

    other_owners_result = await repository.get_by_id_for_user(product.id, intruder.id)
    missing_id_result = await repository.get_by_id_for_user(999_999_994, intruder.id)

    assert other_owners_result is None
    assert missing_id_result is None


@pytest.mark.asyncio
async def test_delete_for_user_removes_the_owners_product_and_returns_true(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)

    assert await repository.delete_for_user(product.id, user.id) is True
    assert await repository.get_by_id_for_user(product.id, user.id) is None


@pytest.mark.asyncio
async def test_delete_for_user_leaves_another_users_product_untouched(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    owner = await make_user(db_session)
    intruder = await make_user(db_session)
    product = await make_product(db_session, user_id=owner.id)

    assert await repository.delete_for_user(product.id, intruder.id) is False
    # The row is untouched -- proven by the owner still being able to see it,
    # not merely by the boolean return value above.
    assert await repository.get_by_id_for_user(product.id, owner.id) is not None


@pytest.mark.asyncio
async def test_delete_for_user_on_a_missing_id_returns_false(
    db_session: AsyncSession,
) -> None:
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    assert await repository.delete_for_user(999_999_993, user.id) is False


@pytest.mark.asyncio
async def test_delete_for_user_cascades_but_does_not_evict_a_preloaded_alert(
    db_session: AsyncSession,
) -> None:
    """Condition (з), stated as a named limit, not a bug: after
    ``delete_for_user``, the dependent ``alerts`` row is gone from
    PostgreSQL (the DB-level ``ON DELETE CASCADE``), but an ``Alert``
    already present in *this* session's identity map before the delete
    stays persistent -- ``synchronize_session="evaluate"`` only
    synchronizes the ``Product`` rows the DELETE statement actually
    matched, and the ORM never sees the cascade at all. Mutating that
    stale ``Alert`` and flushing raises ``StaleDataError``: the caller
    gets a loud abort, not a silent write to a row that no longer exists.
    """
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

    assert await repository.delete_for_user(product.id, user.id) is True

    # DB-level fact, read through a fresh SELECT rather than the ORM's
    # identity map, so this genuinely reflects what PostgreSQL did.
    remaining = await db_session.execute(
        text("SELECT id FROM alerts WHERE id = :id"), {"id": alert.id}
    )
    assert remaining.scalars().all() == []  # cascade removed the row

    # The stale Alert is still tracked by this session (not evicted) --
    # exactly the limit PROJECT.md section 8.3 names.
    assert alert in db_session.sync_session

    alert.target_price = 12_345
    with pytest.raises(StaleDataError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_delete_for_user_evicts_the_deleted_product_from_the_identity_map(
    db_session: AsyncSession,
) -> None:
    """Condition (и): eviction, distinguished from both a tautology and
    from mere expiry.

    ``session.get(...) is None`` alone is a tautology, exactly as a bare
    ``SELECT`` would be (AC5): the DB row really is gone either way,
    regardless of synchronization strategy, so that assertion by itself
    cannot tell "evicted" apart from "merely expired, but still in the
    identity map". Measured directly on a copy outside the repository: an
    object that is deleted at the DB level and then only ``expire()``'d
    (simulating that hypothesis) still answers ``get() -> None`` -- but
    stays ``in`` the session, and a plain, unawaited attribute read on it
    raises ``MissingGreenlet`` instead of returning a value, because
    expired attributes need an async round trip to repopulate.
    ``delete_for_user``'s real ``synchronize_session="evaluate"`` does
    neither: the matched ``Product`` is removed from the identity map
    outright (``session._remove_newly_deleted``), so a plain attribute
    read on the same, still-held Python object succeeds with no lazy load
    at all. See the next test for the ``synchronize_session=False``
    contrast this one is a positive control for (convention 22).
    """
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    product_id = product.id

    assert await repository.delete_for_user(product_id, user.id) is True

    assert product not in db_session.sync_session  # evicted, not merely expired
    assert product.product_name is not None  # no lazy load -> no MissingGreenlet
    assert await db_session.get(Product, product_id) is None


@pytest.mark.asyncio
async def test_synchronize_session_false_would_leave_a_stale_object_cached(
    db_session: AsyncSession,
) -> None:
    """Negative control for the previous test (convention 22): proves that
    ``session.get(...) is None`` is actually sensitive to the
    synchronization strategy ``delete_for_user`` uses, rather than being
    true regardless of it. Never calls ``ProductRepository`` -- this
    issues its own Core ``DELETE`` with a *different*
    ``synchronize_session`` value, entirely inside this test, purely to
    show what an unnoticed regression in that one keyword argument would
    look like: measured directly, a plain ``session.get()`` afterwards
    returns the same, now-stale, still-fully-populated Python object
    instead of ``None``.
    """
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    product_id = product.id

    stmt = (
        delete(Product)
        .where(Product.id == product_id)
        .execution_options(synchronize_session=False)
    )
    await db_session.execute(stmt)

    stale = await db_session.get(Product, product_id)

    assert stale is not None
    assert stale is product


@pytest.mark.asyncio
async def test_delete_for_user_issues_exactly_one_round_trip(
    db_session: AsyncSession,
) -> None:
    """Condition (л): counts ``before_cursor_execute`` events on the real
    ``Engine`` behind this fixture's session (convention 25), rather than
    trusting the docstring's "without an extra round trip" claim by
    reading it. Guards against a regression to a naive load-then-delete
    implementation (``get_by_id_for_user`` + ``session.delete(obj)`` +
    ``flush()``) -- measured directly at three round trips here, because
    ``Product.alerts``' ``cascade="all, delete-orphan"`` makes the ORM
    load the alerts collection before it can emit the ``DELETE``.

    This does **not** catch a regression to
    ``synchronize_session="fetch"``, and the assertion on the statement
    text right below exists because of that gap, measured directly:
    on this dialect, ``fetch`` costs *zero* extra round trips -- it
    appends ``RETURNING products.id`` to the same single ``DELETE``
    instead of issuing a separate ``SELECT`` first (PostgreSQL supports
    ``RETURNING`` on ``DELETE``; a dialect without it would need the
    extra round trip this test's title would then correctly describe).
    ``evaluate`` never asks for that clause at all, which is the one
    textual difference this test can hold onto.
    """
    repository = ProductRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)

    calls: list[tuple[str, bool]] = []

    def _record_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        calls.append((str(statement), executemany))

    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record_cursor_execute)
    try:
        assert await repository.delete_for_user(product.id, user.id) is True
    finally:
        event.remove(engine, "before_cursor_execute", _record_cursor_execute)

    assert len(calls) == 1  # exactly one round trip, not a load-then-delete sequence
    statement, executemany = calls[0]
    assert executemany is False
    assert "RETURNING" not in statement  # evaluate; a "fetch" regression adds it


@pytest.mark.asyncio
async def test_list_paginated_rejects_a_limit_above_max_page_size(
    db_session: AsyncSession,
) -> None:
    """Condition 4 of the Description, exercised here too (not just for
    ``User``): the base class's own bound, not something either concrete
    repository redefines.
    """
    repository = ProductRepository(db_session)

    with pytest.raises(InvalidPaginationError):
        await repository.list_paginated(offset=0, limit=repository.MAX_PAGE_SIZE + 1)
