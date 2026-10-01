"""Tests for the PAB-068 domain exceptions (AC3)."""

import pytest

from app.domain.exceptions import DomainError
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import InvalidPriceInputError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.exceptions import ProductRefParseError


def test_product_ref_parse_error_keeps_reason_and_message() -> None:
    error = ProductRefParseError("unsupported host")

    assert error.reason == "unsupported host"
    assert str(error) == "Cannot parse product reference: unsupported host"


def test_invalid_price_input_error_keeps_reason_and_message() -> None:
    error = InvalidPriceInputError("price is too large")

    assert error.reason == "price is too large"
    assert str(error) == "Invalid price input: price is too large"


def test_product_limit_exceeded_error_carries_the_limit() -> None:
    error = ProductLimitExceededError(50)

    assert error.limit == 50
    assert str(error) == "Tracked products limit exceeded: limit=50"


def test_duplicate_alert_error_message() -> None:
    assert str(DuplicateAlertError()) == (
        "Alert with the same product and target price exists"
    )


def test_marketplace_not_supported_error_keeps_marketplace_and_message() -> None:
    error = MarketplaceNotSupportedError("ozon")

    assert error.marketplace == "ozon"
    assert str(error) == "Marketplace is not supported: ozon"


@pytest.mark.parametrize(
    "error",
    [
        ProductRefParseError("r"),
        InvalidPriceInputError("r"),
        ProductLimitExceededError(1),
        DuplicateAlertError(),
        MarketplaceNotSupportedError("ozon"),
    ],
)
def test_business_errors_share_the_domain_error_base(error: Exception) -> None:
    assert isinstance(error, DomainError)
