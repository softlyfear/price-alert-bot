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


class ProductFailure(StrEnum):
    """Last persisted fetch failure of a tracked product (PROJECT.md 2.6).

    The members mirror `app.schemas.marketplace.FetchFailureReason`: persistence
    must not depend on the marketplace contract module, and a guard test keeps
    the two value sets equal.
    """

    not_found = "not_found"
    blocked = "blocked"
    transport_error = "transport_error"
    bad_payload = "bad_payload"
    out_of_stock = "out_of_stock"
