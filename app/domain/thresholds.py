"""Alert direction rule: derived from the threshold versus the current price."""

from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.models.enums import AlertDirection


def choose_direction(current_price: int, target_price: int) -> AlertDirection:
    """Pick the alert direction for a threshold; both prices are kopecks.

    A threshold below the current price means "notify when cheaper", above
    means "notify when pricier". An equal threshold is ambiguous and rejected.
    """
    if target_price < current_price:
        return AlertDirection.below
    if target_price > current_price:
        return AlertDirection.above
    raise TargetEqualsCurrentPriceError
