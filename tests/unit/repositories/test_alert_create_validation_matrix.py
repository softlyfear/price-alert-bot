"""PAB-058 condition (д): the full validation matrix of ``AlertRepository.create``.

Everything asserted here happens *before* ``SQLAlchemyRepository.create()``
ever touches the session -- unknown-field, missing-field, and
required-non-nullable checks are pure operations on ``set``/``Mapping``, so
PAB-058's Description places this matrix at unit level, on a session
double, rather than on the live database. What ``refresh()`` does with a
real ``server_default`` (PAB-011 condition (b), PAB-008's actual product
claim) is a separate, PostgreSQL-only test in
``tests/integration/repositories/test_alert_repository.py``.

``AlertRepository`` currently declares the same four fields for
``_create_fields``, ``_required_fields``, and ``_required_non_nullable_fields``
(``user_id``, ``product_id``, ``target_price``, ``direction``), which is
exactly the shape PAB-008 broke: with ``is_active`` folded back into
``_required_non_nullable_fields`` (see
``tests/unit/repositories/test_field_set_invariants.py`` for the guard and
its mutation), a payload carrying exactly these four fields would raise
``RequiredFieldCannotBeNoneError({'is_active'})`` instead of succeeding --
see the report for that mutation applied against the success case below.
"""

from typing import Any
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import call

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import InvalidCreateFieldsError
from app.domain.exceptions import MissingRequiredCreateFieldsError
from app.domain.exceptions import RequiredFieldCannotBeNoneError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.repositories.alert import AlertRepository

_VALID_PAYLOAD: dict[str, Any] = {
    "user_id": 1,
    "product_id": 2,
    "target_price": 1_500,
    "direction": AlertDirection.below,
}

#: Every field ``AlertRepository`` currently requires -- present in
#: ``_create_fields``, ``_required_fields``, and
#: ``_required_non_nullable_fields`` alike, so each is exercised once for
#: "missing" and once for "present but None".
_REQUIRED_FIELDS = tuple(_VALID_PAYLOAD)


class _SessionCallRecorder:
    """Session double recording exactly the calls a successful ``create()``
    makes: one ``add()``, one ``flush()``, one ``refresh()`` -- and nothing
    that would compile SQL or reach a socket. Cast to ``AsyncSession`` at
    the call site below; nothing here needs to actually be one.
    """

    def __init__(self) -> None:
        self.add = Mock()
        self.flush = AsyncMock()
        self.refresh = AsyncMock()


def _make_repository() -> tuple[AlertRepository, _SessionCallRecorder]:
    recorder = _SessionCallRecorder()
    repository = AlertRepository(cast(AsyncSession, recorder))
    return repository, recorder


@pytest.mark.asyncio
async def test_create_succeeds_with_exactly_the_four_required_fields() -> None:
    """The one success cell of the matrix: exactly the four required fields.

    Checks the call shape the Description names explicitly: one ``add()``
    with an ``Alert`` instance, one ``flush()``, one ``refresh()`` of that
    same instance, and the same object handed back to the caller.
    """
    repository, recorder = _make_repository()

    result = await repository.create(dict(_VALID_PAYLOAD))

    recorder.add.assert_called_once()
    (added_obj,), _ = recorder.add.call_args
    assert isinstance(added_obj, Alert)
    assert result is added_obj
    recorder.flush.assert_awaited_once_with()
    assert recorder.refresh.await_args_list == [call(added_obj)]


@pytest.mark.parametrize("missing_field", _REQUIRED_FIELDS)
@pytest.mark.asyncio
async def test_create_raises_missing_required_field_error_for_each_absent_field(
    missing_field: str,
) -> None:
    """Dropping any one required field is rejected before the session is touched."""
    repository, recorder = _make_repository()
    payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != missing_field}

    with pytest.raises(MissingRequiredCreateFieldsError) as excinfo:
        await repository.create(payload)

    assert excinfo.value.missing_fields == {missing_field}
    recorder.add.assert_not_called()
    recorder.flush.assert_not_awaited()


@pytest.mark.parametrize("none_field", _REQUIRED_FIELDS)
@pytest.mark.asyncio
async def test_create_raises_required_field_cannot_be_none_error_for_each_field(
    none_field: str,
) -> None:
    """Every required field is also non-nullable: ``None`` is rejected too,
    distinctly from a field being absent, and names a one-element set
    (convention 1) -- unlike on ``HEAD`` before PAB-008, where the set was
    two-element for a single offending field.
    """
    repository, recorder = _make_repository()
    payload = {**_VALID_PAYLOAD, none_field: None}

    with pytest.raises(RequiredFieldCannotBeNoneError) as excinfo:
        await repository.create(payload)

    assert excinfo.value.fields == {none_field}
    recorder.add.assert_not_called()
    recorder.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_raises_invalid_fields_error_for_server_only_fields() -> None:
    """``is_active`` and ``triggered_at`` are server/patch-only fields
    (PROJECT.md section 8.3: ``is_active`` comes from ``server_default``,
    ``triggered_at`` is set only by the notification path) and are rejected
    together at creation. Checked via the ``unknown_fields`` attribute
    (a real ``set``, compared by value) rather than the rendered message,
    so convention 1's warning about unordered ``repr()`` output on a
    multi-element set never applies here.
    """
    repository, recorder = _make_repository()
    payload = {**_VALID_PAYLOAD, "is_active": True, "triggered_at": None}

    with pytest.raises(InvalidCreateFieldsError) as excinfo:
        await repository.create(payload)

    assert excinfo.value.unknown_fields == {"is_active", "triggered_at"}
    recorder.add.assert_not_called()
    recorder.flush.assert_not_awaited()
