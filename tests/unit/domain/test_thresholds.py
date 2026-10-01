"""Tests for app.domain.thresholds.choose_direction (PAB-072 AC1).

Expected values are hand-written literals in kopecks, one row per boundary.
"""

import pytest

from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.domain.money import MAX_PRICE_KOPECKS
from app.domain.thresholds import choose_direction
from app.models.enums import AlertDirection


@pytest.mark.parametrize(
    ("current", "target", "expected"),
    [
        (15_000, 14_999, AlertDirection.below),
        (15_000, 15_001, AlertDirection.above),
        (15_000, 9_000, AlertDirection.below),
        (15_000, 20_000, AlertDirection.above),
        (2, 1, AlertDirection.below),
        (15_000, 1, AlertDirection.below),
        (15_000, MAX_PRICE_KOPECKS, AlertDirection.above),
        (MAX_PRICE_KOPECKS, MAX_PRICE_KOPECKS - 1, AlertDirection.below),
        (1, 2, AlertDirection.above),
    ],
    ids=[
        "one-kopeck-below",
        "one-kopeck-above",
        "far-below",
        "far-above",
        "target-1-current-2",
        "target-1-current-15000",
        "target-max-price",
        "current-max-target-below",
        "current-1-target-2",
    ],
)
def test_direction_follows_the_threshold_versus_the_current_price(
    current: int, target: int, expected: AlertDirection
) -> None:
    assert choose_direction(current, target) is expected


@pytest.mark.parametrize("price", [1, 15_000, MAX_PRICE_KOPECKS])
def test_equal_threshold_has_no_direction(price: int) -> None:
    with pytest.raises(TargetEqualsCurrentPriceError):
        choose_direction(price, price)
