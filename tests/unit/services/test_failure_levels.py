"""Tests for app.services.failure_levels (PAB-069 AC5, PROJECT.md section 8.4)."""

import pytest

from app.schemas.marketplace import FetchFailureReason
from app.services.failure_levels import failure_log_level

# Hand-copied from the PROJECT.md section 8.4 table, not derived from the code.
_TABLE_8_4: dict[FetchFailureReason, str] = {
    FetchFailureReason.bad_payload: "ERROR",
    FetchFailureReason.blocked: "WARNING",
    FetchFailureReason.transport_error: "WARNING",
    FetchFailureReason.not_found: "WARNING",
    FetchFailureReason.out_of_stock: "INFO",
}


@pytest.mark.parametrize(("reason", "level"), list(_TABLE_8_4.items()))
def test_failure_log_level_matches_the_section_8_4_table(
    reason: FetchFailureReason, level: str
) -> None:
    assert failure_log_level(reason) == level


def test_section_8_4_table_covers_every_failure_reason() -> None:
    """A new `FetchFailureReason` member must be added to the table above and to
    the policy together; this fails if only one of them learns about it."""
    assert set(_TABLE_8_4) == set(FetchFailureReason)
