"""Tests for app.domain.links.extract_product_ref (PAB-107, PROJECT.md R19).

Expected values are hand-written literals; "the earlier error" is obtained from
the strict ``parse_product_ref`` itself, which is the contract of the ticket.
"""

import pytest

from app.domain.exceptions import MultipleProductRefsError
from app.domain.exceptions import ProductRefParseError
from app.domain.links import MAX_INPUT_LENGTH
from app.domain.links import ProductRef
from app.domain.links import extract_product_ref
from app.domain.links import parse_product_ref
from app.models.enums import Marketplace

WB = Marketplace.wb
OZON = Marketplace.ozon
_NBSP = " "
_TITLE = "Брюки чиносы прямые без стрелок черные Westrenger"
_WB_LINK = "https://www.wildberries.ru/catalog/13511022/detail.aspx?size=41036292"
_OZON_LINK = "https://www.ozon.ru/product/nazvanie-tovara-123456789/?at=abc"
_EDGE_CHARS = "«»\"'()[]<>,.;:!?…"
_WB_REF = ProductRef(WB, 13511022)


def test_customer_share_text_with_newline_gives_the_wildberries_product() -> None:
    assert extract_product_ref(f"{_TITLE}\n{_WB_LINK}") == _WB_REF


@pytest.mark.parametrize("sep", [" ", _NBSP, "\t", "\r\n", "\n\n"])
def test_any_whitespace_between_title_and_link_works(sep: str) -> None:
    assert extract_product_ref(f"{_TITLE}{sep}{_WB_LINK}") == _WB_REF


def test_link_first_then_title_works() -> None:
    assert extract_product_ref(f"{_WB_LINK}\n{_TITLE}") == _WB_REF


@pytest.mark.parametrize("char", list(_EDGE_CHARS))
def test_each_edge_character_around_the_link_is_stripped(char: str) -> None:
    assert extract_product_ref(f"{_TITLE} {char}{_WB_LINK}{char}") == _WB_REF


@pytest.mark.parametrize(
    "text",
    [
        f"{_TITLE} {_WB_LINK}.",
        f"{_TITLE} ({_WB_LINK})",
        f"{_TITLE} «{_WB_LINK}»",
        f"{_TITLE} [{_WB_LINK}], спасибо",
    ],
)
def test_link_in_punctuation_gives_the_same_product(text: str) -> None:
    assert extract_product_ref(text) == _WB_REF


def test_ozon_share_text_gives_the_ozon_product() -> None:
    text = f"Название товара\n{_OZON_LINK}"

    assert extract_product_ref(text) == ProductRef(OZON, 123456789)


def test_the_same_link_twice_is_one_product() -> None:
    assert extract_product_ref(f"{_WB_LINK}\n{_WB_LINK}") == _WB_REF


def test_two_hosts_of_the_same_article_are_one_product() -> None:
    text = (
        "https://wb.ru/catalog/13511022/detail.aspx "
        "https://www.wildberries.ru/catalog/13511022/detail.aspx"
    )

    assert extract_product_ref(text) == _WB_REF


def test_two_different_wildberries_products_are_refused() -> None:
    text = f"{_WB_LINK} https://www.wildberries.ru/catalog/777/detail.aspx"

    with pytest.raises(MultipleProductRefsError) as info:
        extract_product_ref(text)

    assert info.value.count == 2
    assert isinstance(info.value, ProductRefParseError)


def test_wildberries_and_ozon_products_are_refused() -> None:
    with pytest.raises(MultipleProductRefsError) as info:
        extract_product_ref(f"{_WB_LINK}\n{_OZON_LINK}")

    assert info.value.count == 2


def test_same_article_on_two_marketplaces_is_two_products() -> None:
    text = (
        "https://www.wildberries.ru/catalog/123456789/detail.aspx "
        "https://www.ozon.ru/product/x-123456789/"
    )

    with pytest.raises(MultipleProductRefsError):
        extract_product_ref(text)


def test_count_is_the_number_of_distinct_products() -> None:
    text = (
        f"{_WB_LINK} {_WB_LINK} {_OZON_LINK} "
        "https://www.wildberries.ru/catalog/5/detail.aspx"
    )

    with pytest.raises(MultipleProductRefsError) as info:
        extract_product_ref(text)

    assert info.value.count == 3


@pytest.mark.parametrize(
    "text",
    [
        "Купить 13511022 шт",
        "Цена 1990 ₽",
        "Товар 5 13511022",
        "Название https://ozon.ru/t/AbCd12",
        "Название https://wildberries.ru.evil.com/catalog/1/detail.aspx",
        "Название https://evil.com/?u=https://www.wildberries.ru/catalog/1/detail.aspx",
        "просто слова без ссылок",
        "Название https://www.wildberries.ru/catalog/0/detail.aspx",
    ],
)
def test_text_without_a_product_link_raises_the_whole_text_error(text: str) -> None:
    with pytest.raises(ProductRefParseError) as whole:
        parse_product_ref(text)

    with pytest.raises(ProductRefParseError) as extracted:
        extract_product_ref(text)

    assert type(extracted.value) is ProductRefParseError
    assert str(extracted.value) == str(whole.value)


def test_bare_number_inside_a_sentence_is_not_an_article() -> None:
    with pytest.raises(ProductRefParseError, match="whitespace"):
        extract_product_ref("Купить 13511022 шт")


def test_bare_number_next_to_a_link_does_not_count_as_a_second_product() -> None:
    assert extract_product_ref(f"Купить 777 шт\n{_WB_LINK}") == _WB_REF


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "https://www.wildberries.ru/catalog/12345678/detail.aspx",
            ProductRef(WB, 12345678),
        ),
        (
            "https://wildberries.ru/catalog/12345678/detail.aspx",
            ProductRef(WB, 12345678),
        ),
        ("https://wb.ru/catalog/12345678/detail.aspx", ProductRef(WB, 12345678)),
        ("https://www.wb.ru/catalog/12345678/detail.aspx", ProductRef(WB, 12345678)),
        (
            "http://www.wildberries.ru/catalog/12345678/detail.aspx",
            ProductRef(WB, 12345678),
        ),
        ("www.wildberries.ru/catalog/12345678/detail.aspx", ProductRef(WB, 12345678)),
        ("wb.ru/catalog/7/detail.aspx", ProductRef(WB, 7)),
        (
            "https://www.wildberries.ru/catalog/12345678/detail.aspx?targetUrl=GP#x",
            ProductRef(WB, 12345678),
        ),
        ("HTTPS://WWW.WILDBERRIES.RU/catalog/5/detail.aspx", ProductRef(WB, 5)),
        ("12345678", ProductRef(WB, 12345678)),
        ("13511022", _WB_REF),
        ("  12345678  ", ProductRef(WB, 12345678)),
        ("\n12345678\t", ProductRef(WB, 12345678)),
        ("9223372036854775807", ProductRef(WB, 9_223_372_036_854_775_807)),
        (
            "https://www.ozon.ru/product/krossovki-muzhskie-1234567/",
            ProductRef(OZON, 1234567),
        ),
        (
            "https://ozon.ru/product/krossovki-muzhskie-1234567/",
            ProductRef(OZON, 1234567),
        ),
        ("https://www.ozon.ru/product/1234567/", ProductRef(OZON, 1234567)),
        ("https://www.ozon.ru/product/1234567", ProductRef(OZON, 1234567)),
        (
            "https://www.ozon.ru/product/slug-with-dashes-42/?at=abc",
            ProductRef(OZON, 42),
        ),
        ("ozon.ru/product/9/", ProductRef(OZON, 9)),
    ],
)
def test_every_input_the_strict_parser_accepts_gives_the_same_result(
    text: str, expected: ProductRef
) -> None:
    assert parse_product_ref(text) == expected
    assert extract_product_ref(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_or_blank_text_is_refused_as_empty(text: str) -> None:
    with pytest.raises(ProductRefParseError, match="input is empty") as info:
        extract_product_ref(text)

    assert type(info.value) is ProductRefParseError


def test_text_over_the_limit_is_refused_before_splitting_even_with_a_link() -> None:
    text = f"{_TITLE}\n{_WB_LINK}" + " " * MAX_INPUT_LENGTH
    assert len(text) > MAX_INPUT_LENGTH

    with pytest.raises(ProductRefParseError, match="input is too long"):
        extract_product_ref(text)


def test_text_exactly_at_the_limit_still_yields_the_link() -> None:
    head = f"{_TITLE}\n{_WB_LINK}"
    text = head + " " * (MAX_INPUT_LENGTH - len(head))
    assert len(text) == MAX_INPUT_LENGTH

    assert extract_product_ref(text) == _WB_REF


def test_one_character_over_the_limit_is_refused() -> None:
    head = f"{_TITLE}\n{_WB_LINK}"
    text = head + " " * (MAX_INPUT_LENGTH + 1 - len(head))
    assert len(text) == MAX_INPUT_LENGTH + 1

    with pytest.raises(ProductRefParseError, match="input is too long"):
        extract_product_ref(text)


@pytest.mark.parametrize("junk", ["###", "https://evil.com/x", "ozon.ru/t/Ab", "😀"])
def test_junk_token_next_to_a_valid_link_is_ignored(junk: str) -> None:
    assert extract_product_ref(f"{junk} {_WB_LINK} {junk}") == _WB_REF


def test_multiple_refs_error_keeps_the_count_and_is_a_parse_error() -> None:
    error = MultipleProductRefsError(4)

    assert error.count == 4
    assert isinstance(error, ProductRefParseError)
    assert "4" in str(error)


def test_over_the_limit_text_reaches_the_strict_parser_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def counting(text: str) -> ProductRef:
        calls.append(text)
        return parse_product_ref(text)

    monkeypatch.setattr("app.domain.links.parse_product_ref", counting)
    head = f"{_TITLE}\n{_WB_LINK}"
    text = head + " " * (MAX_INPUT_LENGTH + 1 - len(head))

    with pytest.raises(ProductRefParseError, match="input is too long"):
        extract_product_ref(text)

    assert len(calls) == 1


@pytest.mark.parametrize(
    "link",
    [
        "https://www.wildberries.ru@evil.com/catalog/1/detail.aspx",
        "https://evil.com@www.wildberries.ru/catalog/1/detail.aspx",
        "https://www.wildberries.ru:8443/catalog/1/detail.aspx",
    ],
)
def test_authority_tricks_in_a_share_text_are_not_extracted(link: str) -> None:
    with pytest.raises(ProductRefParseError) as info:
        extract_product_ref(f"{_TITLE}\n{link}")

    assert type(info.value) is ProductRefParseError
