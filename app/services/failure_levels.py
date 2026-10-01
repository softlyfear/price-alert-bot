"""Single map of marketplace fetch failure reasons to log levels (PROJECT.md 8.4)."""

from typing import assert_never

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
