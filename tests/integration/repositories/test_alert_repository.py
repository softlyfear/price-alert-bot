"""Integration tests for app.repositories.alert against a live PostgreSQL instance.

Carries PAB-011 condition (b) -- the main PAB-008 claim, undoable on
mocks: ``AlertRepository.create`` with exactly the four required fields
returns an object whose ``is_active`` is ``True`` after the base class's
``flush()``/``refresh()``, and that value came from PostgreSQL's
``server_default``, not from any Python-side default (``Alert.is_active``
declares only ``server_default=text("true")`` -- no ``default=``, see
``app/models/alert.py``).

Also carries the ownership-scoped methods (PAB-058 Description item 2), the
``rowcount`` matrix (condition (к)) for both ``deactivate`` and
``delete_for_user``, including the repeat-``deactivate`` cell and its
``mut_rowcount`` mutation guard (PAB-058 AC6) -- see the report for the
mutation run -- and, for ``delete_for_user``, the same identity-map
eviction differentiator (condition (и)) and round-trip count (condition
(л)) as ``test_product_repository.py``'s equivalent method, so the two
identically-worded docstrings ("evicts ... without an extra round trip")
do not silently diverge.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.repositories.alert import AlertRepository
from tests.integration.repositories.conftest import make_alert
from tests.integration.repositories.conftest import make_product
from tests.integration.repositories.conftest import make_user


@pytest.mark.asyncio
async def test_create_with_the_four_required_fields_gets_is_active_from_server_default(
    db_session: AsyncSession,
) -> None:
    """PAB-011 condition (b). ``Alert.is_active`` has no Python-side
    default -- constructing one directly, without going through the
    repository, leaves the attribute unset until the database is
    consulted, which is what makes ``True`` here attributable to
    PostgreSQL rather than to the class definition.
    """
    unpersisted = Alert(
        user_id=1, product_id=1, target_price=1, direction=AlertDirection.below
    )
    assert "is_active" not in unpersisted.__dict__

    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)

    alert = await repository.create(
        {
            "user_id": user.id,
            "product_id": product.id,
            "target_price": 9_000,
            "direction": AlertDirection.below,
        }
    )

    assert alert.is_active is True


@pytest.mark.asyncio
async def test_get_by_user_and_product_returns_matching_alerts(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    other_product = await make_product(db_session, user_id=user.id)
    matching = await make_alert(db_session, user_id=user.id, product_id=product.id)
    await make_alert(db_session, user_id=user.id, product_id=other_product.id)

    result = await repository.get_by_user_and_product(user.id, product.id)

    assert [a.id for a in result] == [matching.id]


@pytest.mark.asyncio
async def test_get_active_by_product_excludes_inactive_alerts(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    active = await make_alert(
        db_session, user_id=user.id, product_id=product.id, target_price=100
    )
    inactive = await make_alert(
        db_session, user_id=user.id, product_id=product.id, target_price=200
    )
    inactive.is_active = False
    await db_session.flush()

    result = await repository.get_active_by_product(product.id)

    assert [a.id for a in result] == [active.id]


@pytest.mark.asyncio
async def test_patch_updates_only_the_given_field_and_persists_it(
    db_session: AsyncSession,
) -> None:
    """Exercises ``_patch_fields`` (three fields) while only supplying one
    of them, so the base class's ``for field in self._patch_fields: if
    field in patch_data`` loop takes both branches -- not just the
    all-fields-present shape ``test_user_repository.py``'s single-field
    ``_patch_fields`` can never produce.
    """
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

    patched = await repository.patch(alert.id, {"target_price": 4_242})

    assert patched is not None
    assert patched.target_price == 4_242
    assert patched.is_active is True  # untouched field kept its value


@pytest.mark.asyncio
async def test_get_by_id_for_user_hides_another_users_alert(
    db_session: AsyncSession,
) -> None:
    """PROJECT.md section 8.2: another user's row and a missing id give the
    same observable result.
    """
    repository = AlertRepository(db_session)
    owner = await make_user(db_session)
    intruder = await make_user(db_session)
    product = await make_product(db_session, user_id=owner.id)
    alert = await make_alert(db_session, user_id=owner.id, product_id=product.id)

    other_owners_result = await repository.get_by_id_for_user(alert.id, intruder.id)
    missing_id_result = await repository.get_by_id_for_user(999_999_992, intruder.id)

    assert other_owners_result is None
    assert missing_id_result is None


@pytest.mark.asyncio
async def test_get_by_id_for_user_returns_the_owners_alert(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

    found = await repository.get_by_id_for_user(alert.id, user.id)

    assert found is not None
    assert found.id == alert.id


@pytest.mark.asyncio
async def test_deactivate_by_the_owner_returns_true_and_persists(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

    assert await repository.deactivate(alert.id, user.id) is True

    await db_session.refresh(alert)
    assert alert.is_active is False


@pytest.mark.asyncio
async def test_deactivate_by_another_user_returns_false_and_leaves_it_active(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    owner = await make_user(db_session)
    intruder = await make_user(db_session)
    product = await make_product(db_session, user_id=owner.id)
    alert = await make_alert(db_session, user_id=owner.id, product_id=product.id)

    assert await repository.deactivate(alert.id, intruder.id) is False

    await db_session.refresh(alert)
    assert alert.is_active is True


@pytest.mark.asyncio
async def test_deactivate_on_a_missing_id_returns_false(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    assert await repository.deactivate(999_999_991, user.id) is False


@pytest.mark.asyncio
async def test_deactivate_expires_updated_at_requiring_an_explicit_refresh(
    db_session: AsyncSession,
) -> None:
    """``deactivate``'s bulk ``UPDATE`` (``synchronize_session="evaluate"``)
    assigns ``is_active`` explicitly, so ``evaluate`` updates that
    attribute on the matching in-session object directly, in Python, with
    no query. ``updated_at`` is different: nothing in the statement's
    ``SET`` clause names it, so its new value only exists as PostgreSQL's
    own ``onupdate`` expression result -- ``evaluate`` cannot guess it and
    expires the attribute instead of leaving it silently stale. Measured
    directly: a plain, unawaited access to ``alert.updated_at`` right
    after ``deactivate`` raises ``MissingGreenlet``, not a wrong value --
    the caller is forced through an explicit ``await
    session.refresh(...)`` (used below) to observe the real one.
    """
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

    assert await repository.deactivate(alert.id, user.id) is True

    assert alert.is_active is False  # explicitly SET -> evaluated in place, no expiry

    with pytest.raises(MissingGreenlet):
        _ = alert.updated_at  # expired by the onupdate expression, not refreshed

    await db_session.refresh(alert)
    assert alert.updated_at is not None


@pytest.mark.asyncio
async def test_deactivate_on_an_already_inactive_alert_still_returns_true(
    db_session: AsyncSession,
) -> None:
    """Condition (к), the repeat-deactivate cell, as a named behaviour: the
    ``UPDATE``'s ``WHERE`` clause has no ``is_active`` predicate, so
    PostgreSQL counts the row as matched and written even when the value
    being written equals the one already stored -- ``True`` means "the row
    matched and was written", not "the state changed".

    The column *is* still asserted to have been rewritten each time,
    though, via the same differentiator as
    ``test_deactivate_expires_updated_at_requiring_an_explicit_refresh``:
    a plain, unawaited read of ``alert.updated_at`` right after each call
    raises ``MissingGreenlet``, because ``evaluate`` expires it as part of
    the statement's own ``onupdate`` handling. A mutation that dropped
    ``onupdate`` from ``TimestampMixin``, or dropped the column from the
    generated ``SET`` clause, would leave the attribute un-expired and
    this would stop raising -- exactly the class of regression a bare
    ``rowcount``/``is_active`` check cannot see.

    ``updated_at``'s *value* is deliberately **not** asserted to differ
    between the two calls, though. Measured directly on this project's
    PostgreSQL: within a single transaction, ``now()``/``CURRENT_TIMESTAMP``
    -- what ``onupdate=text("now()")`` evaluates --
    returns the transaction's start time on every call, not the
    statement's, so two ``UPDATE``s of the same row inside one transaction
    get the identical ``updated_at``. This fixture gives each test exactly
    one transaction (``tests/integration/conftest.py``) and this suite is
    not permitted a second engine (PAB-058 AC11), so no sequence of calls
    inside one test can observe that column's *value* actually advance;
    doing so would need an intervening real ``COMMIT``, which the
    fixture's rollback-based isolation cannot allow. This is a measured,
    reported limit of the fixture, not evidence that ``deactivate`` fails
    to rewrite the row -- the ``rowcount``-derived ``True`` and the two
    ``MissingGreenlet`` observations above already prove that it does.
    """
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

    assert await repository.deactivate(alert.id, user.id) is True
    with pytest.raises(MissingGreenlet):
        _ = alert.updated_at  # onupdate expired it -> rewritten, not skipped
    await db_session.refresh(alert)
    assert alert.is_active is False

    assert await repository.deactivate(alert.id, user.id) is True
    with pytest.raises(MissingGreenlet):
        _ = alert.updated_at  # rewritten again on the repeat call too
    await db_session.refresh(alert)
    assert alert.is_active is False


@pytest.mark.asyncio
async def test_delete_for_user_by_the_owner_returns_true(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

    assert await repository.delete_for_user(alert.id, user.id) is True
    assert await repository.get_by_id_for_user(alert.id, user.id) is None


@pytest.mark.asyncio
async def test_delete_for_user_by_another_user_returns_false_and_leaves_it(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    owner = await make_user(db_session)
    intruder = await make_user(db_session)
    product = await make_product(db_session, user_id=owner.id)
    alert = await make_alert(db_session, user_id=owner.id, product_id=product.id)

    assert await repository.delete_for_user(alert.id, intruder.id) is False
    assert await repository.get_by_id_for_user(alert.id, owner.id) is not None


@pytest.mark.asyncio
async def test_delete_for_user_on_a_missing_id_returns_false(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    assert await repository.delete_for_user(999_999_990, user.id) is False


@pytest.mark.asyncio
async def test_delete_for_user_evicts_the_deleted_alert_from_the_identity_map(
    db_session: AsyncSession,
) -> None:
    """Same differentiator as
    ``test_product_repository.py``'s equivalent test, applied here so the
    two ``delete_for_user`` docstrings -- which make the identical
    "evicts ... without an extra round trip" claim -- do not silently
    diverge if only one of the two files is ever revisited.

    ``session.get(...) is None`` alone is a tautology (AC5): it cannot
    tell "evicted" apart from "merely expired, but still in the identity
    map". Measured directly on a copy outside the repository: an object
    deleted at the DB level and then only ``expire()``'d (simulating that
    hypothesis) still answers ``get() -> None``, but stays ``in`` the
    session, and a plain, unawaited attribute read on it raises
    ``MissingGreenlet``. The real ``synchronize_session="evaluate"`` does
    neither: the matched ``Alert`` is removed from the identity map
    outright, so a plain attribute read on the same, still-held Python
    object succeeds with no lazy load at all.
    """
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)
    alert_id = alert.id

    assert await repository.delete_for_user(alert_id, user.id) is True

    assert alert not in db_session.sync_session  # evicted, not merely expired
    assert alert.target_price is not None  # no lazy load -> no MissingGreenlet
    assert await db_session.get(Alert, alert_id) is None


@pytest.mark.asyncio
async def test_delete_for_user_issues_exactly_one_round_trip(
    db_session: AsyncSession,
) -> None:
    """Condition (л): counts ``before_cursor_execute`` events on the real
    ``Engine`` behind this fixture's session (convention 25). Guards
    against a regression to a naive load-then-delete implementation
    (``get_by_id_for_user`` + ``session.delete(obj)`` + ``flush()``) --
    measured directly at two round trips (a ``SELECT`` then the
    ``DELETE``) for ``Alert``, which has no dependent relationships to add
    a third the way ``Product``'s does.

    This does **not** catch a regression to
    ``synchronize_session="fetch"``, and the statement-text assertion
    below exists because of that gap, measured directly: on this dialect,
    ``fetch`` costs *zero* extra round trips for a ``DELETE`` -- it
    appends ``RETURNING alerts.id`` to the same single statement instead
    of issuing a separate ``SELECT`` first (PostgreSQL supports
    ``RETURNING`` on ``DELETE``). ``evaluate`` never asks for that clause,
    which is the one textual difference this test can hold onto.
    """
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await make_alert(db_session, user_id=user.id, product_id=product.id)

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
        assert await repository.delete_for_user(alert.id, user.id) is True
    finally:
        event.remove(engine, "before_cursor_execute", _record_cursor_execute)

    assert len(calls) == 1  # exactly one round trip, not a load-then-delete sequence
    statement, executemany = calls[0]
    assert executemany is False
    assert "RETURNING" not in statement  # evaluate; a "fetch" regression adds it


# --- update_target_for_user (PAB-095) -------------------------------------

_TRIGGERED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


async def _stored(
    session: AsyncSession, alert_id: int
) -> tuple[int, AlertDirection, datetime | None, bool]:
    """The row as PostgreSQL holds it (column query: bypasses the identity map)."""
    row = (
        await session.execute(
            select(
                Alert.target_price,
                Alert.direction,
                Alert.triggered_at,
                Alert.is_active,
            ).where(Alert.id == alert_id)
        )
    ).one()
    return (row[0], row[1], row[2], row[3])


async def _triggered_alert(
    session: AsyncSession, *, user_id: int, product_id: int, target: int = 9_000
) -> Alert:
    alert = await make_alert(
        session, user_id=user_id, product_id=product_id, target_price=target
    )
    alert.triggered_at = _TRIGGERED
    await session.flush()
    return alert


@pytest.mark.asyncio
async def test_update_target_for_user_by_the_owner_sets_values_and_clears_triggered_at(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await _triggered_alert(db_session, user_id=user.id, product_id=product.id)
    assert (await _stored(db_session, alert.id))[2] == _TRIGGERED

    changed = await repository.update_target_for_user(
        alert.id, user.id, 25_000, AlertDirection.above
    )

    assert changed is True
    assert await _stored(db_session, alert.id) == (
        25_000,
        AlertDirection.above,
        None,
        True,
    )


@pytest.mark.asyncio
async def test_update_target_for_user_leaves_is_active_false_untouched(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await _triggered_alert(db_session, user_id=user.id, product_id=product.id)
    assert await repository.deactivate(alert.id, user.id) is True

    changed = await repository.update_target_for_user(
        alert.id, user.id, 7_000, AlertDirection.below
    )

    assert changed is True
    assert await _stored(db_session, alert.id) == (
        7_000,
        AlertDirection.below,
        None,
        False,
    )


@pytest.mark.asyncio
async def test_update_target_for_user_refreshes_the_loaded_alert_in_the_same_session(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await _triggered_alert(db_session, user_id=user.id, product_id=product.id)

    await repository.update_target_for_user(
        alert.id, user.id, 25_000, AlertDirection.above
    )

    # Read through the identity map, with no refresh: no stale values.
    reread = await repository.get_by_id_for_user(alert.id, user.id)
    assert reread is alert
    assert (alert.target_price, alert.direction, alert.triggered_at) == (
        25_000,
        AlertDirection.above,
        None,
    )


@pytest.mark.asyncio
async def test_update_target_for_user_by_another_user_returns_false_and_changes_nothing(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    owner = await make_user(db_session)
    intruder = await make_user(db_session)
    product = await make_product(db_session, user_id=owner.id)
    alert = await _triggered_alert(db_session, user_id=owner.id, product_id=product.id)

    changed = await repository.update_target_for_user(
        alert.id, intruder.id, 25_000, AlertDirection.above
    )

    assert changed is False
    assert await _stored(db_session, alert.id) == (
        9_000,
        AlertDirection.below,
        _TRIGGERED,
        True,
    )
    assert alert.target_price == 9_000


@pytest.mark.asyncio
async def test_update_target_for_user_on_a_missing_id_returns_false(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)

    assert (
        await repository.update_target_for_user(
            999_999_992, user.id, 25_000, AlertDirection.above
        )
        is False
    )


@pytest.mark.asyncio
async def test_update_target_for_user_issues_exactly_one_statement(
    db_session: AsyncSession,
) -> None:
    """Convention 25: ``before_cursor_execute`` on the real ``Engine``."""
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await _triggered_alert(db_session, user_id=user.id, product_id=product.id)

    calls: list[tuple[str, bool]] = []

    def _record(
        conn: Any,
        cursor: Any,
        statement: Any,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        calls.append((str(statement), executemany))

    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", _record)
    try:
        assert (
            await repository.update_target_for_user(
                alert.id, user.id, 25_000, AlertDirection.above
            )
            is True
        )
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(calls) == 1
    statement, executemany = calls[0]
    assert executemany is False
    assert statement.lstrip().upper().startswith("UPDATE ALERTS")
    assert "RETURNING" not in statement


@pytest.mark.asyncio
async def test_update_target_for_user_colliding_with_another_alert_raises_keeping_rows(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    first = await make_alert(
        db_session, user_id=user.id, product_id=product.id, target_price=5_000
    )
    second = await _triggered_alert(
        db_session, user_id=user.id, product_id=product.id, target=6_000
    )

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await repository.update_target_for_user(
                second.id, user.id, 5_000, AlertDirection.below
            )

    assert "uq_alerts_user_product_logic" in str(excinfo.value)
    assert (await _stored(db_session, first.id))[:2] == (5_000, AlertDirection.below)
    assert await _stored(db_session, second.id) == (
        6_000,
        AlertDirection.below,
        _TRIGGERED,
        True,
    )
    assert second.target_price == 6_000


@pytest.mark.asyncio
async def test_update_target_for_user_with_a_negative_target_violates_the_check(
    db_session: AsyncSession,
) -> None:
    repository = AlertRepository(db_session)
    user = await make_user(db_session)
    product = await make_product(db_session, user_id=user.id)
    alert = await _triggered_alert(db_session, user_id=user.id, product_id=product.id)

    with pytest.raises(IntegrityError) as excinfo:
        async with db_session.begin_nested():
            await repository.update_target_for_user(
                alert.id, user.id, -1, AlertDirection.below
            )

    assert "ck_alerts_target_price_non_negative" in str(excinfo.value)
    assert (await _stored(db_session, alert.id))[:3] == (
        9_000,
        AlertDirection.below,
        _TRIGGERED,
    )
