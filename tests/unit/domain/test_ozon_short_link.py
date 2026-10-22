"""Ozon short links `ozon.ru/t/<code>` in the domain (PAB-114, R22 items 1 and 3).

Expected values are written out by hand from the rules of R22: host from the
Ozon set, path exactly `/t/<code>`, code = 1..32 ASCII letters and digits.
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import MultipleProductRefsError
from app.domain.exceptions import ProductRefParseError
from app.domain.links import MAX_INPUT_LENGTH
from app.domain.links import OzonShortLink
from app.domain.links import ProductRef
from app.domain.links import extract_product_ref
from app.domain.links import ozon_short_link
from app.domain.links import parse_ozon_product_location
from app.domain.links import parse_product_ref
from app.models.enums import Marketplace

_SHORT = OzonShortLink("7GeNMx2")
_FULL = "https://www.ozon.ru/product/some-slug-2622563074/"


@pytest.mark.parametrize("code", ["a", "7GeNMx2", "Z9", "0", "a" * 32])
def test_ozon_short_link_accepts_ascii_alnum_up_to_32(code: str) -> None:
    assert ozon_short_link(code) == OzonShortLink(code)


@pytest.mark.parametrize(
    "code",
    ["", "a" * 33, "a-b", "a_b", "a%41", "код", "a b", "a/b", "a.b", "١٢٣", "²", "a\n"],
)
def test_ozon_short_link_rejects_everything_else(code: str) -> None:
    with pytest.raises(ProductRefParseError):
        ozon_short_link(code)


@pytest.mark.parametrize(
    "text",
    [
        "https://ozon.ru/t/7GeNMx2",
        "https://www.ozon.ru/t/7GeNMx2/",
        "https://ozon.ru/t/7GeNMx2?utm_source=share#frag",
        "ozon.ru/t/7GeNMx2",
        "www.ozon.ru/t/7GeNMx2/",
        "http://ozon.ru/t/7GeNMx2",  # same scheme rule as a full link
    ],
)
def test_parse_product_ref_recognises_a_short_link(text: str) -> None:
    assert parse_product_ref(text) == _SHORT


@pytest.mark.parametrize(
    "text",
    [
        "https://ozon.ru/t/7GeNMx2",
        "Название\nhttps://ozon.ru/t/7GeNMx2",
        "Смотри, что нашёл: https://www.ozon.ru/t/7GeNMx2/ .",
        "https://ozon.ru/t/7GeNMx2 https://ozon.ru/t/7GeNMx2",
    ],
)
def test_extract_product_ref_recognises_a_short_link(text: str) -> None:
    assert extract_product_ref(text) == _SHORT


@pytest.mark.parametrize(
    "text",
    [
        "https://ozon.ru/t/7GeNMx2/y",
        "https://ozon.ru.evil.com/t/7GeNMx2",
        "https://ozon.com/t/7GeNMx2",
        "https://wildberries.ru/t/7GeNMx2",
        "https://www.wildberries.ru/t/7GeNMx2",
        "https://user@ozon.ru/t/7GeNMx2",
        "https://ozon.ru:8443/t/7GeNMx2",
        "https://ozon.ru/t/",
        "https://ozon.ru/t",
        "https://ozon.ru/t/a-b",
        "https://ozon.ru/t/" + "a" * 33,
        "https://ozon.ru/t/код",
    ],
)
def test_parse_product_ref_refuses_a_malformed_short_link(text: str) -> None:
    with pytest.raises(ProductRefParseError):
        parse_product_ref(text)


@pytest.mark.parametrize(
    "text",
    [
        "Название https://ozon.ru/t/7GeNMx2/y",
        "Название https://ozon.ru.evil.com/t/7GeNMx2",
        "Название https://ozon.ru:8443/t/7GeNMx2",
    ],
)
def test_extract_product_ref_refuses_a_malformed_short_link(text: str) -> None:
    with pytest.raises(ProductRefParseError):
        extract_product_ref(text)


def test_short_and_full_ozon_link_in_one_text_are_multiple_refs() -> None:
    with pytest.raises(MultipleProductRefsError) as info:
        extract_product_ref(f"https://ozon.ru/t/7GeNMx2 {_FULL}")

    assert info.value.count == 2


def test_two_different_short_links_are_multiple_refs() -> None:
    with pytest.raises(MultipleProductRefsError) as info:
        extract_product_ref("https://ozon.ru/t/AAA https://ozon.ru/t/BBB")

    assert info.value.count == 2


def test_bare_digits_and_full_links_still_parse_as_before() -> None:
    assert parse_product_ref(_FULL) == ProductRef(Marketplace.ozon, 2622563074)


# --- parse_ozon_product_location ---------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "https://www.ozon.ru/product/some-slug-2622563074/?at=TOKEN&short=7GeNMx2",
        "https://ozon.ru/product/some-slug-2622563074/",
        "https://www.ozon.ru/product/2622563074/?from=share",
    ],
)
def test_location_of_a_product_gives_an_ozon_ref(location: str) -> None:
    assert parse_ozon_product_location(location) == ProductRef(
        Marketplace.ozon, 2622563074
    )


@pytest.mark.parametrize(
    "location",
    [
        "http://www.ozon.ru/product/x-2622563074/",
        "/product/x-2622563074/",
        "//ozon.ru/product/x-2622563074/",
        "ozon.ru/product/x-2622563074/",
        "https:ozon.ru/product/x-2622563074/",
        "https://evil.com/product/x-2622563074/",
        "https://ozon.ru.evil.com/product/x-2622563074/",
        "https://user@www.ozon.ru/product/x-2622563074/",
        "https://www.ozon.ru@evil.com/product/x-2622563074/",
        "https://www.ozon.ru:8443/product/x-2622563074/",
        "https://www.ozon.ru/category/telefony-15501/",
        "https://www.ozon.ru/t/7GeNMx2",
        "https://www.ozon.ru/product/x-2622563074/ y",
        "https://www.ozon.ru/product/x-2622563074/\x07",
        "https://[",
        "",
    ],
)
def test_location_that_is_not_an_ozon_product_is_refused(location: str) -> None:
    with pytest.raises(ProductRefParseError):
        parse_ozon_product_location(location)


def test_location_over_the_length_limit_is_refused_and_at_it_is_not() -> None:
    prefix = "https://www.ozon.ru/product/x-2622563074/?q="
    at_limit = prefix + "a" * (MAX_INPUT_LENGTH - len(prefix))
    assert len(at_limit) == MAX_INPUT_LENGTH

    assert parse_ozon_product_location(at_limit).article == 2622563074
    with pytest.raises(ProductRefParseError):
        parse_ozon_product_location(at_limit + "a")
