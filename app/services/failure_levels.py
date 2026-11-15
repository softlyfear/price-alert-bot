"""Single map of marketplace fetch failure reasons to log levels (PROJECT.md 8.4)."""

from typing import assert_never

from app.models.enums import ProductFailure
from app.schemas.marketplace import FetchFailureReason


def failure_log_level(reason: FetchFailureReason) -> str:
    """Return the loguru level name for a fetch failure reason.

    The match is exhaustive: a new `FetchFailureReason` member fails `mypy`
    here, in the one place that owns the policy.
    """
    match reason:
        case FetchFailureReason.bad_payload:
            return "ERROR"
        case FetchFailureReason.out_of_stock:
            return "INFO"
        case (
            FetchFailureReason.blocked
            | FetchFailureReason.transport_error
            | FetchFailureReason.not_found
        ):
            return "WARNING"
        case _:
            assert_never(reason)


def product_failure(reason: FetchFailureReason) -> ProductFailure:
    """Map a fetch failure to the persisted product status.

    The value sets are kept equal by `ProductFailure`'s guard test; a new
    reason without a persistence member fails here with `ValueError` instead of
    being stored silently.
    """
    return ProductFailure(reason.value)
