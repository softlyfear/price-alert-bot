"""Bounds and compatibility of WB option data."""

import pytest
from pydantic import ValidationError

from app.schemas.marketplace import MarketplaceProductData
from app.schemas.marketplace import WbSizeData
from app.schemas.wb import WbSize


@pytest.mark.parametrize("value", [0, -1, True, "42", 9223372036854775808])
def test_upstream_option_id_must_be_a_positive_bounded_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        WbSize.model_validate({"optionId": value})


def test_legacy_size_and_unpriced_identified_size_are_valid() -> None:
    assert WbSize.model_validate({}).option_id is None
    assert WbSize.model_validate({"optionId": 42, "name": "42"}).price is None
    assert MarketplaceProductData(
        name="Item", price=100, wb_sizes={42: WbSizeData(name="42")}
    ).wb_sizes == {42: WbSizeData(name="42", price=None)}


@pytest.mark.parametrize("value", [0, -1, True, "100", 2147483648])
def test_size_price_must_be_valid_kopecks(value: object) -> None:
    with pytest.raises(ValidationError):
        WbSizeData.model_validate({"price": value})


@pytest.mark.parametrize("value", [0, -1, True, "42", 9223372036854775808])
def test_size_map_key_must_be_valid(value: object) -> None:
    with pytest.raises(ValidationError):
        MarketplaceProductData.model_validate(
            {"name": "Item", "price": 100, "wb_sizes": {value: {}}}
        )
