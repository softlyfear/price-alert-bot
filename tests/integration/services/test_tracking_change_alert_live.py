"""``TrackingService.change_alert_target`` on a real PostgreSQL (PAB-095).

What only a real database can show: the ownership predicate in the single
``UPDATE``, ``triggered_at`` reset, "no ``UPDATE`` at all" for an unchanged
threshold (statements counted with ``before_cursor_execute``, convention 25),
the unique key firing inside the SAVEPOINT while the outer transaction stays
usable, and a statement count independent of the number of thresholds.
Isolation comes from the ``db_session`` fixture (outer transaction, rolled
back after each test).
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import cast

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.repositories.alert import AlertRepository
from app.services.base_client import BaseMarketplaceClient
from app.services.tracking import AlertChange
from app.services.tracking import TrackingService

pytestmark = pytest.mark.asyncio

_WB = Marketplace.wb
_tg_ids = itertools.count(720_000_001)
_articles = itertools.count(7_000_001)
_PRICE = 15_000  # current price stored with the product
_TRIGGERED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _NoFetchClient(BaseMarketplaceClient):
    async def get_product_data(self, article: int) -> Any:
        raise AssertionError("changing a threshold must not call the marketplace")


def _service(session: AsyncSession) -> TrackingService:
    return TrackingService(
        session=session,
        http_client=httpx.AsyncClient(),
        client_factory=lambda _m, _h: _NoFetchClient(),
        max_products_per_user=100,
    )


async def _add(service: TrackingService, tg: int, article: int, target: int) -> Alert:
    return await service.add_tracking(tg, _WB, article, "Live item", _PRICE, target)


async def _mark_triggered(session: AsyncSession, alert_id: int) -> None:
    await session.execute(
        update(Alert).where(Alert.id == alert_id).values(triggered_at=_TRIGGERED)
    )


async def _stored(
    session: AsyncSession, alert_id: int
) -> tuple[int, AlertDirection, datetime | None]:
    row = (
        await session.execute(
            select(Alert.target_price, Alert.direction, Alert.triggered_at).where(
                Alert.id == alert_id
            )
        )
    ).one()
    return (row[0], row[1], row[2])


async def _statements(
    session: AsyncSession, action: Callable[[], Awaitable[object]]
) -> list[str]:
    """SQL texts issued by ``action`` on the real ``Engine`` behind the session."""
    calls: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any,
        executemany: bool,
    ) -> None:  # fmt: skip
        calls.append(str(statement))

    engine = session.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return calls


async def _count_alerts(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Alert))).scalar_one()


async def test_changed_flips_below_to_above_and_resets_triggered_at(
    db_session: AsyncSession,
) -> None:
    service, tg = _service(db_session), next(_tg_ids)
    alert = await _add(service, tg, next(_articles), 9_000)
    await _mark_triggered(db_session, alert.id)
    assert await _stored(db_session, alert.id) == (
        9_000,
        AlertDirection.below,
        _TRIGGERED,
    )

    outcome = await service.change_alert_target(tg, alert.id, 20_000)

    assert outcome is AlertChange.changed
    assert await _stored(db_session, alert.id) == (20_000, AlertDirection.above, None)


async def test_unchanged_issues_no_update_and_keeps_triggered_at(
    db_session: AsyncSession,
) -> None:
    service, tg = _service(db_session), next(_tg_ids)
    alert = await _add(service, tg, next(_articles), 9_000)
    await _mark_triggered(db_session, alert.id)
    outcomes: list[AlertChange] = []

    async def act() -> None:
        outcomes.append(await service.change_alert_target(tg, alert.id, 9_000))

    statements = await _statements(db_session, act)

    assert outcomes == [AlertChange.unchanged]
    assert statements  # the reads did run
    assert not [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
    assert await _stored(db_session, alert.id) == (
        9_000,
        AlertDirection.below,
        _TRIGGERED,
    )


@pytest.mark.parametrize("who", ["foreign", "missing"])
async def test_not_found_for_someone_elses_or_a_missing_alert_changes_nothing(
    db_session: AsyncSession, who: str
) -> None:
    service = _service(db_session)
    owner, intruder = next(_tg_ids), next(_tg_ids)
    alert = await _add(service, owner, next(_articles), 9_000)
    await _mark_triggered(db_session, alert.id)
    target_id = alert.id if who == "foreign" else 999_999_993

    outcome = await service.change_alert_target(intruder, target_id, 20_000)

    assert outcome is AlertChange.not_found
    assert await _stored(db_session, alert.id) == (
        9_000,
        AlertDirection.below,
        _TRIGGERED,
    )


async def test_target_equal_to_the_current_price_is_refused_and_the_row_is_intact(
    db_session: AsyncSession,
) -> None:
    service, tg = _service(db_session), next(_tg_ids)
    alert = await _add(service, tg, next(_articles), 9_000)
    await _mark_triggered(db_session, alert.id)

    with pytest.raises(TargetEqualsCurrentPriceError):
        await service.change_alert_target(tg, alert.id, _PRICE)

    assert await _stored(db_session, alert.id) == (
        9_000,
        AlertDirection.below,
        _TRIGGERED,
    )


async def test_duplicate_of_another_threshold_is_refused_and_the_session_survives(
    db_session: AsyncSession,
) -> None:
    service, tg, article = _service(db_session), next(_tg_ids), next(_articles)
    await _add(service, tg, article, 9_000)
    second = await _add(service, tg, article, 8_000)

    with pytest.raises(DuplicateAlertError):
        await service.change_alert_target(tg, second.id, 9_000)

    assert (await _stored(db_session, second.id))[:2] == (8_000, AlertDirection.below)
    assert await _count_alerts(db_session) == 2  # session still executes statements
    await db_session.commit()


async def _first_call_sees_nothing(
    monkeypatch: pytest.MonkeyPatch, owner: type, method: str, empty: Any
) -> None:
    """Make the first call of ``owner.method`` return ``empty``, later ones real."""
    real = cast(Callable[..., Awaitable[Any]], getattr(owner, method))
    calls: list[None] = []

    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(None)
        if len(calls) == 1:
            return empty
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(owner, method, wrapper)


async def test_key_race_inside_the_savepoint_becomes_duplicate_error_and_session_lives(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check is blind (a concurrent request committed in the gap): the
    unique key fires inside the SAVEPOINT and is reported as a duplicate."""
    service, tg, article = _service(db_session), next(_tg_ids), next(_articles)
    await _add(service, tg, article, 9_000)
    second = await _add(service, tg, article, 8_000)
    await _mark_triggered(db_session, second.id)
    await _first_call_sees_nothing(
        monkeypatch, AlertRepository, "get_by_user_and_product", []
    )

    with pytest.raises(DuplicateAlertError) as excinfo:
        await service.change_alert_target(tg, second.id, 9_000)

    assert isinstance(excinfo.value.__cause__, IntegrityError)
    assert await _stored(db_session, second.id) == (
        8_000,
        AlertDirection.below,
        _TRIGGERED,
    )
    assert await _count_alerts(db_session) == 2
    await db_session.commit()


async def test_integrity_error_that_is_not_a_duplicate_propagates_and_session_lives(
    db_session: AsyncSession,
) -> None:
    """A negative target passes the direction rule and fails the CHECK."""
    service, tg = _service(db_session), next(_tg_ids)
    alert = await _add(service, tg, next(_articles), 9_000)

    with pytest.raises(IntegrityError) as excinfo:
        await service.change_alert_target(tg, alert.id, -1)

    assert "ck_alerts_target_price_non_negative" in str(excinfo.value)
    assert (await _stored(db_session, alert.id))[:2] == (9_000, AlertDirection.below)
    assert await _count_alerts(db_session) == 1
    await db_session.commit()


async def test_statement_count_is_the_same_for_1_and_30_thresholds(
    db_session: AsyncSession,
) -> None:
    service = _service(db_session)
    small, big = next(_tg_ids), next(_tg_ids)
    one = await _add(service, small, next(_articles), 9_000)
    big_article = next(_articles)
    first = await _add(service, big, big_article, 1_000)
    for target in range(1_001, 1_030):
        await _add(service, big, big_article, target)

    with_one = await _statements(
        db_session, lambda: service.change_alert_target(small, one.id, 9_500)
    )
    with_thirty = await _statements(
        db_session, lambda: service.change_alert_target(big, first.id, 9_500)
    )

    assert len(with_thirty) == len(with_one)
    assert len([s for s in with_thirty if s.lstrip().upper().startswith("UPDATE")]) == 1
