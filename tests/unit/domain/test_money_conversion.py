"""Tests for rubles/kopecks conversion and price formatting (PAB-068 AC2).

Expected values are hand-computed literals taken from the ticket.
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.exceptions import InvalidPriceInputError
from app.domain.money import MAX_PRICE_KOPECKS
from app.domain.money import format_price
from app.domain.money import rubles_to_kopecks
from app.schemas.marketplace import MarketplaceProductData

_NBSP = "\u00a0"
_MONEY_SOURCE = Path(__file__).resolve().parents[3] / "app" / "domain" / "money.py"


@pytest.mark.parametrize(
    ("text", "kopecks"),
    [
        ("1990", 199000),
        ("1990,5", 199050),
        ("1990.5", 199050),
        ("1990,50", 199050),
        ("1 990.99", 199099),
        (f"1{_NBSP}990,99", 199099),
        ("1 234 567", 123456700),
        ("  1990  ", 199000),
        ("\t1990\n", 199000),
        ("0,01", 1),
        ("0.1", 10),
        ("1", 100),
        ("007", 700),
        ("21474836,47", 2_147_483_647),
        ("21 474 836.47", 2_147_483_647),
        ("21474836", 2_147_483_600),
    ],
)
def test_rubles_to_kopecks_accepts_valid_input(text: str, kopecks: int) -> None:
    assert rubles_to_kopecks(text) == kopecks


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "0",
        "0,00",
        "0.0",
        "000",
        "-5",
        "-0,01",
        "+5",
        "1990,555",
        "1990.123",
        "1990,",
        ",5",
        ".5",
        "1990,5,5",
        "1,990.50",
        "inf",
        "nan",
        "Infinity",
        "-inf",
        "1e3",
        "1E3",
        "0x10",
        "abc",
        "199O",
        "1990₽",
        "₽1990",
        "1990 руб",
        "1990р",
        "1  990",
        "1990 000,",
        "1_990",
        "١٩٩٠",
        "1990\n5",
        "21474836,48",
        "21474837",
        "99999999999999999999",
        "1" * 33,
    ],
)
def test_rubles_to_kopecks_rejects_invalid_input(text: str) -> None:
    with pytest.raises(InvalidPriceInputError):
        rubles_to_kopecks(text)


def test_price_of_exactly_max_kopecks_is_accepted_and_one_more_is_rejected() -> None:
    assert rubles_to_kopecks("21474836,47") == MAX_PRICE_KOPECKS
    with pytest.raises(InvalidPriceInputError):
        rubles_to_kopecks("21474836,48")


def test_input_of_32_chars_is_accepted_and_33_rejected_before_parsing() -> None:
    thirty_two = "0" * 29 + "1,5"
    thirty_three = "0" + thirty_two
    assert len(thirty_two) == 32
    assert rubles_to_kopecks(thirty_two) == 150
    with pytest.raises(InvalidPriceInputError):
        rubles_to_kopecks(thirty_three)


def test_overlong_input_is_rejected_even_if_padding_is_whitespace() -> None:
    with pytest.raises(InvalidPriceInputError):
        rubles_to_kopecks("5" + " " * 40)


def test_price_error_does_not_echo_raw_input() -> None:
    with pytest.raises(InvalidPriceInputError) as excinfo:
        rubles_to_kopecks("SECRET-MARKER")

    assert "SECRET" not in str(excinfo.value)
    assert "SECRET" not in excinfo.value.reason


def test_max_price_kopecks_is_the_int4_maximum() -> None:
    assert MAX_PRICE_KOPECKS == 2_147_483_647


def test_marketplace_product_price_accepts_max_price_kopecks() -> None:
    """Guard: the two int4 bounds (domain and schema) must not drift."""
    data = MarketplaceProductData(name="Item", price=MAX_PRICE_KOPECKS)

    assert data.price == MAX_PRICE_KOPECKS


def test_marketplace_product_price_rejects_max_price_kopecks_plus_one() -> None:
    with pytest.raises(ValidationError):
        MarketplaceProductData(name="Item", price=MAX_PRICE_KOPECKS + 1)


def test_money_module_has_no_float_on_the_conversion_path() -> None:
    source = _MONEY_SOURCE.read_text(encoding="utf-8")

    assert re.search(r"\bfloat\b|\bDecimal\b", source) is None


@pytest.mark.parametrize(
    ("kopecks", "expected"),
    [
        (199000, f"1{_NBSP}990{_NBSP}₽"),
        (199050, f"1{_NBSP}990,50{_NBSP}₽"),
        (0, f"0{_NBSP}₽"),
        (1, f"0,01{_NBSP}₽"),
        (99, f"0,99{_NBSP}₽"),
        (100, f"1{_NBSP}₽"),
        (99900, f"999{_NBSP}₽"),
        (100000, f"1{_NBSP}000{_NBSP}₽"),
        (123456789, f"1{_NBSP}234{_NBSP}567,89{_NBSP}₽"),
        (MAX_PRICE_KOPECKS, f"21{_NBSP}474{_NBSP}836,47{_NBSP}₽"),
    ],
)
def test_format_price(kopecks: int, expected: str) -> None:
    assert format_price(kopecks) == expected


def test_format_price_uses_no_plain_space() -> None:
    assert " " not in format_price(123456789)


@pytest.mark.parametrize("kopecks", [-1, -199000])
def test_format_price_rejects_negative(kopecks: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        format_price(kopecks)
