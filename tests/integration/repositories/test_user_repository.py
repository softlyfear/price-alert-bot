"""Integration tests for app.repositories.user against a live PostgreSQL instance.

Also carries the generic ``SQLAlchemyRepository`` create/patch/delete/
list_paginated coverage for PAB-058 condition 3 of the Description
("Обобщённые ``create``/``patch``/``delete`` базового
``SQLAlchemyRepository`` -- включая ``flush()`` и ``refresh()``"): ``User``
is the vehicle because it has the fewest required fields and no cascading
relationships to reason about, keeping those tests about the base class's
own contract rather than about ``User``-specific behaviour. The
``AlertRepository``-specific claim of PAB-011 condition (b) --
``is_active`` arriving from ``server_default`` -- has its own test in
``test_alert_repository.py``, since ``User`` has no such column.

The create/missing-field/None-field/unknown-field validation matrix itself
is unit-tested (PAB-058 condition (d), against ``AlertRepository``) and is
not repeated here against the live database: it is pure logic that never
touches the session before raising. What repeats here is exactly what
unit tests cannot prove -- that ``flush()``/``refresh()`` deliver real,
server-computed values onto the Python object.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EmptyPatchError
from app.domain.exceptions import GetOrCreateUserError
from app.domain.exceptions import InvalidPaginationError
from app.domain.exceptions import InvalidPatchFieldsError
from app.models.user import User
from app.repositories.user import UserRepository
from tests.integration.repositories.conftest import make_user


@pytest.mark.asyncio
async def test_get_or_create_by_tg_id_creates_a_new_user_when_none_exists(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    tg_user_id = 950_000_001

    user, created = await repository.get_or_create_by_tg_id(tg_user_id)

    assert created is True
    assert user.id is not None
    assert user.tg_user_id == tg_user_id


@pytest.mark.asyncio
async def test_get_or_create_by_tg_id_returns_the_same_row_on_conflict(
    db_session: AsyncSession,
) -> None:
    """The one method in the project implementing ``INSERT ... ON CONFLICT
    DO NOTHING`` with a fallback ``SELECT`` (PAB-058 Description item 1):
    the conflict branch is unreachable on mocks, since it depends on a
    real unique-constraint violation. Both calls run in this same test's
    transaction/session, so the second one's ``INSERT`` genuinely
    conflicts with the row the first one committed to the transaction.
    """
    repository = UserRepository(db_session)
    tg_user_id = 950_000_002

    first_user, first_created = await repository.get_or_create_by_tg_id(tg_user_id)
    second_user, second_created = await repository.get_or_create_by_tg_id(tg_user_id)

    assert first_created is True
    assert second_created is False
    assert second_user.id == first_user.id
    # Same session -> the fallback SELECT's result is identity-mapped to
    # the very same Python object the first call returned, not merely an
    # equal-by-id copy.
    assert second_user is first_user


@pytest.mark.asyncio
async def test_get_or_create_by_tg_id_raises_if_the_fallback_select_finds_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive branch: the ``INSERT ... ON CONFLICT DO NOTHING`` found a
    real conflict (a genuine pre-existing row makes its ``RETURNING``
    give nothing), but the fallback ``SELECT`` then *also* finds nothing.
    Under READ COMMITTED with a unique index backing the conflict target,
    the very row that caused the conflict cannot vanish between these two
    statements inside the same transaction, so there is no real
    interleaving that reaches this branch -- it is redirected here by
    substituting a query for a value that is guaranteed not to exist,
    rather than by an actual race.
    """
    repository = UserRepository(db_session)
    tg_user_id = 950_000_099
    await make_user(db_session, tg_user_id=tg_user_id)  # the real conflicting row

    real_execute = db_session.execute

    async def redirect_select_to_a_value_that_cannot_exist(
        statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if isinstance(statement, Select):
            statement = select(User).where(User.tg_user_id == -1)
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(
        db_session, "execute", redirect_select_to_a_value_that_cannot_exist
    )

    with pytest.raises(GetOrCreateUserError) as excinfo:
        await repository.get_or_create_by_tg_id(tg_user_id)

    assert excinfo.value.tg_user_id == tg_user_id


@pytest.mark.asyncio
async def test_create_persists_user_and_populates_server_default_timestamps(
    db_session: AsyncSession,
) -> None:
    """Base ``create()`` success path: one ``add``, one ``flush``, one
    ``refresh`` (already checked by call count at unit level against
    ``AlertRepository``); here the live database proves the *values* that
    round trip are real -- ``created_at``/``updated_at`` have no Python-side
    default, only ``server_default`` (``app/models/base.py``), so a
    populated, timezone-aware value can only have come from PostgreSQL.
    """
    repository = UserRepository(db_session)

    user = await repository.create({"tg_user_id": 950_000_010})

    assert user.id is not None
    assert user.created_at is not None
    assert user.created_at.tzinfo is not None
    assert user.updated_at is not None
    assert user.updated_at.tzinfo is not None


@pytest.mark.asyncio
async def test_get_by_id_returns_the_matching_row(db_session: AsyncSession) -> None:
    repository = UserRepository(db_session)
    user = await make_user(db_session)

    fetched = await repository.get_by_id(user.id)

    assert fetched is not None
    assert fetched.id == user.id


@pytest.mark.asyncio
async def test_get_by_id_returns_none_for_a_missing_id(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    assert await repository.get_by_id(999_999_997) is None


@pytest.mark.asyncio
async def test_patch_updates_field_and_persists_the_change(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    user = await make_user(db_session)

    patched = await repository.patch(user.id, {"tg_username": "new_handle"})

    assert patched is not None
    assert patched.tg_username == "new_handle"
    # Cross-checked with a plain SELECT, bypassing the identity map, so
    # this is checking what `flush()` actually sent to PostgreSQL, not
    # just what the Python object was told to hold.
    row = await db_session.execute(select(User.tg_username).where(User.id == user.id))
    assert row.scalar_one() == "new_handle"


@pytest.mark.asyncio
async def test_patch_on_a_missing_id_returns_none(db_session: AsyncSession) -> None:
    repository = UserRepository(db_session)
    assert await repository.patch(999_999_996, {"tg_username": "ghost"}) is None


@pytest.mark.asyncio
async def test_patch_with_an_unknown_field_raises_invalid_patch_fields_error(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    user = await make_user(db_session)

    with pytest.raises(InvalidPatchFieldsError) as excinfo:
        await repository.patch(user.id, {"tg_user_id": 1})

    assert excinfo.value.unknown_fields == {"tg_user_id"}


@pytest.mark.asyncio
async def test_patch_with_an_empty_payload_raises_empty_patch_error(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    user = await make_user(db_session)

    with pytest.raises(EmptyPatchError):
        await repository.patch(user.id, {})


@pytest.mark.asyncio
async def test_delete_removes_the_row_and_returns_true(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    user = await make_user(db_session)

    assert await repository.delete(user.id) is True
    assert await repository.get_by_id(user.id) is None


@pytest.mark.asyncio
async def test_delete_on_a_missing_id_returns_false(db_session: AsyncSession) -> None:
    repository = UserRepository(db_session)
    assert await repository.delete(999_999_995) is False


@pytest.mark.asyncio
async def test_list_paginated_rejects_a_negative_offset(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    with pytest.raises(InvalidPaginationError):
        await repository.list_paginated(offset=-1, limit=10)


@pytest.mark.asyncio
async def test_list_paginated_rejects_a_limit_below_one(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    with pytest.raises(InvalidPaginationError):
        await repository.list_paginated(offset=0, limit=0)


@pytest.mark.asyncio
async def test_list_paginated_rejects_a_limit_above_max_page_size(
    db_session: AsyncSession,
) -> None:
    repository = UserRepository(db_session)
    with pytest.raises(InvalidPaginationError):
        await repository.list_paginated(offset=0, limit=repository.MAX_PAGE_SIZE + 1)


@pytest.mark.asyncio
async def test_list_paginated_accepts_boundary_limits_ordered_by_id(
    db_session: AsyncSession,
) -> None:
    """Both ends of the accepted range (condition 4): ``limit=1`` (the
    lower bound) and ``limit=MAX_PAGE_SIZE`` (the upper bound, the same
    value that is rejected one higher in the test above) are both
    accepted, and results come back ordered by ``id``.

    ``offset`` is derived from a row count taken *before* creating this
    test's three rows, rather than hardcoded at ``0``. PAB-011's own
    fixture docstring warns that ``TEST_DATABASE_URL`` can point at a
    database this suite does not own: on a ``users`` table that already
    has >= ``MAX_PAGE_SIZE`` rows (reproduced directly with 120
    pre-existing rows: ``1 failed`` on the hardcoded-``offset=0`` version
    of this test, because ``offset=0, limit=MAX_PAGE_SIZE`` then returns
    only pre-existing rows and never reaches the three created here), a
    fixed ``offset=0`` silently stops proving anything about this test's
    own rows.
    """
    repository = UserRepository(db_session)
    existing_count = (
        await db_session.execute(select(func.count(User.id)))
    ).scalar_one()

    first = await make_user(db_session)
    second = await make_user(db_session)
    third = await make_user(db_session)

    full_page = await repository.list_paginated(
        offset=existing_count, limit=repository.MAX_PAGE_SIZE
    )
    assert [u.id for u in full_page] == [first.id, second.id, third.id]

    one_row_page = await repository.list_paginated(offset=existing_count, limit=1)
    assert [u.id for u in one_row_page] == [first.id]
