"""Enum marketplace and alert."""

from enum import StrEnum


class AlertDirection(StrEnum):
    """Directional trigger for price threshold alerts."""

    below = "below"
    above = "above"


class Marketplace(StrEnum):
    """Supported e-commerce platforms for product tracking."""

    wb = "wb"
    ozon = "ozon"
