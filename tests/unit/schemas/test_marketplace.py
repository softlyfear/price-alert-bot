"""Unit tests for app.schemas.marketplace.

All tests are pure: no I/O, no network, no database, no asyncio. They
validate pydantic model behaviour only.
"""

import pytest
from pydantic import ValidationError

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData

# Reference set of (member name, member value) pairs per PROJECT.md section
# 2.6. A flat set of bare values would miss a swap between two existing
# values (name diverges from its own value while the overall value set is
# unchanged) - see the mutation demonstration in the ticket report.
_EXPECTED_REASON_MEMBERS = frozenset(
    {
        ("not_found", "not_found"),
        ("blocked", "blocked"),
        ("transport_error", "transport_error"),
        ("bad_payload", "bad_payload"),
    }
)


def test_fetch_failure_reason_members_match_reference() -> None:
    """Exactly four members, values equal to names - catches an extra
    member, a missing member, and a name/value mismatch alike (AC1)."""
    actual = frozenset((member.name, member.value) for member in FetchFailureReason)
    assert actual == _EXPECTED_REASON_MEMBERS


def test_fetch_failure_reason_str_yields_bare_value() -> None:
    """`StrEnum` gives the bare value in `str()`, unlike `str, Enum`. This
    is what ends up in the `reason` field of a structured log record."""
    assert str(FetchFailureReason.blocked) == "blocked"


def test_marketplace_fetch_failure_rejects_extra_field() -> None:
    """`extra="forbid"` rejects an unknown field with a `ValidationError`
    of type `extra_forbidden` (AC2)."""
    with pytest.raises(ValidationError) as exc_info:
        MarketplaceFetchFailure(reason=FetchFailureReason.blocked, unknown="x")  # type: ignore[call-arg]

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def test_marketplace_fetch_failure_rejects_reassignment() -> None:
    """`frozen=True` rejects attribute reassignment with a `ValidationError`
    of type `frozen_instance` - a distinct error from `extra_forbidden`
    (AC2)."""
    failure = MarketplaceFetchFailure(reason=FetchFailureReason.blocked)

    with pytest.raises(ValidationError) as exc_info:
        failure.reason = FetchFailureReason.not_found  # type: ignore[misc]

    assert exc_info.value.errors()[0]["type"] == "frozen_instance"


@pytest.mark.parametrize("reason", list(FetchFailureReason))
def test_marketplace_fetch_failure_accepts_every_reason(
    reason: FetchFailureReason,
) -> None:
    """Every one of the four enum members is a valid `reason` (AC3)."""
    failure = MarketplaceFetchFailure(reason=reason)
    assert failure.reason is reason


def test_marketplace_fetch_failure_rejects_reason_outside_enum() -> None:
    """A value not in `FetchFailureReason` is rejected with a `ValidationError`
    of type `enum` (AC3)."""
    with pytest.raises(ValidationError) as exc_info:
        MarketplaceFetchFailure(reason="not_a_real_reason")  # type: ignore[arg-type]

    assert exc_info.value.errors()[0]["type"] == "enum"


def test_marketplace_fetch_failure_detail_defaults_to_none() -> None:
    """`detail` is optional and absent by default."""
    failure = MarketplaceFetchFailure(reason=FetchFailureReason.transport_error)
    assert failure.detail is None


def test_marketplace_fetch_failure_detail_stores_given_text() -> None:
    """`detail` stores the log-facing text when provided."""
    failure = MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="unexpected schema"
    )
    assert failure.detail == "unexpected schema"


def test_fetch_result_alias_accepts_product_data() -> None:
    """`MarketplaceFetchResult` accepts `MarketplaceProductData` - checked
    both at runtime and, via the variable annotation below, under
    `mypy --strict` (AC4)."""
    value: MarketplaceFetchResult = MarketplaceProductData(name="Kettle", price=199900)
    assert isinstance(value, MarketplaceProductData)


def test_fetch_result_alias_accepts_fetch_failure() -> None:
    """`MarketplaceFetchResult` accepts `MarketplaceFetchFailure` - checked
    both at runtime and, via the variable annotation below, under
    `mypy --strict` (AC4)."""
    value: MarketplaceFetchResult = MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked
    )
    assert isinstance(value, MarketplaceFetchFailure)


def test_marketplace_product_data_accepts_valid_payload() -> None:
    """Pre-existing model, untouched by this ticket - covered here only
    because the coverage gate (AC8) scopes the whole file."""
    product = MarketplaceProductData(name="Kettle", price=199900)
    assert product.name == "Kettle"
    assert product.price == 199900


def test_marketplace_product_data_rejects_extra_field() -> None:
    """Pre-existing `extra="forbid"` behaviour, exercised for the same
    coverage-scope reason as above."""
    with pytest.raises(ValidationError) as exc_info:
        MarketplaceProductData(name="Kettle", price=199900, unknown="x")  # type: ignore[call-arg]

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"
