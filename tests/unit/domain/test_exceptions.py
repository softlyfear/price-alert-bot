"""Tests for app.domain.exceptions.

Each test asserts the exception type, the rendered message, and the
attributes stored by ``__init__`` — checking the type alone would leave
the body of ``__init__``, where all the logic lives, uncovered.
"""

import pytest

from app.domain.exceptions import EmptyPatchError
from app.domain.exceptions import GetOrCreateUserError
from app.domain.exceptions import InvalidCreateFieldsError
from app.domain.exceptions import InvalidPaginationError
from app.domain.exceptions import InvalidPatchFieldsError
from app.domain.exceptions import MissingRequiredCreateFieldsError
from app.domain.exceptions import RequiredFieldCannotBeNoneError


def test_invalid_create_fields_error_stores_unknown_fields_and_message() -> None:
    with pytest.raises(InvalidCreateFieldsError) as excinfo:
        raise InvalidCreateFieldsError({"foo"})

    assert excinfo.value.unknown_fields == {"foo"}
    assert str(excinfo.value) == "Unknown fields for creation: {'foo'}"


def test_invalid_patch_fields_error_stores_unknown_fields_and_message() -> None:
    with pytest.raises(InvalidPatchFieldsError) as excinfo:
        raise InvalidPatchFieldsError({"baz"})

    assert excinfo.value.unknown_fields == {"baz"}
    assert str(excinfo.value) == "Unknown fields for patch: {'baz'}"


def test_get_or_create_user_error_stores_tg_user_id_and_message() -> None:
    with pytest.raises(GetOrCreateUserError) as excinfo:
        raise GetOrCreateUserError(123456789)

    assert excinfo.value.tg_user_id == 123456789
    assert str(excinfo.value) == (
        "User with tg_user_id=123456789 not found after insert attempt"
    )


def test_invalid_pagination_error_stores_offset_and_limit_and_message() -> None:
    with pytest.raises(InvalidPaginationError) as excinfo:
        raise InvalidPaginationError(offset=-1, limit=1000, max_page_size=50)

    assert excinfo.value.offset == -1
    assert excinfo.value.limit == 1000
    assert str(excinfo.value) == (
        "Invalid pagination: offset=-1 (must be >= 0), "
        "limit=1000 (must be between 1 and 50)"
    )


def test_missing_required_create_fields_error_stores_missing_fields() -> None:
    with pytest.raises(MissingRequiredCreateFieldsError) as excinfo:
        raise MissingRequiredCreateFieldsError({"tg_user_id"})

    assert excinfo.value.missing_fields == {"tg_user_id"}
    assert str(excinfo.value) == (
        "Missing required fields for creation: {'tg_user_id'}"
    )


def test_empty_patch_error_message() -> None:
    with pytest.raises(EmptyPatchError) as excinfo:
        raise EmptyPatchError()

    assert str(excinfo.value) == "Patch data cannot be empty"


def test_required_field_cannot_be_none_error_stores_fields_and_message() -> None:
    with pytest.raises(RequiredFieldCannotBeNoneError) as excinfo:
        raise RequiredFieldCannotBeNoneError({"price"})

    assert excinfo.value.fields == {"price"}
    assert str(excinfo.value) == "Required fields cannot be None: {'price'}"


def test_missing_required_create_fields_error_lists_all_fields_order_independent() -> (
    None
):
    """Multi-element sets have no guaranteed repr order, so this test checks
    composition (prefix + membership of every field) rather than an exact
    string, unlike the single-element test above which pins the full string.
    """
    exc = MissingRequiredCreateFieldsError({"tg_user_id", "username"})

    assert exc.missing_fields == {"tg_user_id", "username"}
    assert str(exc).startswith("Missing required fields for creation: ")
    assert all(f"'{f}'" in str(exc) for f in exc.missing_fields)
