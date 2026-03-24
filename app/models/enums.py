from enum import Enum


class AlertDirection(Enum):
    """Enum for price alerts."""

    BELOW = "below"
    ABOVE = "above"


class Marketplace(Enum):
    """Enam for chose marketplace."""

    WB = "wb"
    OZON = "ozon"
