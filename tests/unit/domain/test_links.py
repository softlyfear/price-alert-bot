"""Tests for app.domain.links (PAB-068 AC1).

Every row of the tables is one case named in the ticket; expected values are
hand-written literals, not recomputed with the parser's own logic.
"""

import pytest

from app.domain.exceptions import ProductRefParseError
from app.domain.links import MAX_ARTICLE
from app.domain.links import ProductRef
from app.domain.links import build_product_url
from app.domain.links import parse_product_ref
from app.models.enums import Marketplace

WB = Marketplace.wb
OZON = Marketplace.ozon


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
        ("  12345678  ", ProductRef(WB, 12345678)),
        ("\n12345678\t", ProductRef(WB, 12345678)),
        (str(MAX_ARTICLE), ProductRef(WB, 9_223_372_036_854_775_807)),
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
def test_parse_product_ref_accepts_supported_inputs(
    text: str, expected: ProductRef
) -> None:
    assert parse_product_ref(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\t",
        "https://wildberries.ru.evil.com/catalog/1/detail.aspx",
        "https://evilwildberries.ru/catalog/1/detail.aspx",
        "https://notozon.ru/product/1/",
        "https://ozon.ru.evil.com/product/1/",
        "https://ozon.ru@evil.com/product/1/",
        "https://evil.com/wildberries.ru/catalog/1/detail.aspx",
        "https://evil.com/?u=https://www.wildberries.ru/catalog/1/detail.aspx",
        "https://www.ozon.ru:8080/product/1/",
        "https://user:pw@www.wildberries.ru/catalog/1/detail.aspx",
        "javascript:alert(1)",
        "ftp://www.wildberries.ru/catalog/1/detail.aspx",
        "https://www.ozon.ru/t/AbCdEf",
        "https://ozon.ru/t/AbCdEf",
        "https://www.ozon.ru/category/telefony-15501/",
        "https://www.ozon.ru/product/",
        "https://www.ozon.ru/product/a/b-1/",
        "https://www.wildberries.ru/",
        "https://www.wildberries.ru/catalog/12345678/",
        "https://www.wildberries.ru/catalog/12345678/detail.html",
        "https://www.wildberries.ru/catalog/12345678/detail.aspx/extra",
        "https://www.wildberries.ru/brands/12345678/detail.aspx",
        "https://www.wildberries.ru/catalog/abc/detail.aspx",
        "https://www.wildberries.ru/catalog/0/detail.aspx",
        "https://www.wildberries.ru/catalog/-5/detail.aspx",
        "https://www.wildberries.ru/catalog/+5/detail.aspx",
        "https://www.wildberries.ru/catalog/12 34/detail.aspx",
        "https://www.ozon.ru/product/slug-abc/",
        "https://www.ozon.ru/product/slug-0/",
        "0",
        "000",
        "-5",
        "+5",
        "12 34",
        "12345678 87654321",
        "12.5",
        "1e5",
        "abc",
        "١٢٣",
        "https://www.wildberries.ru/catalog/12 34/detail.aspx",
        "https://[::1/",
        str(MAX_ARTICLE + 1),
        f"https://www.wildberries.ru/catalog/{MAX_ARTICLE + 1}/detail.aspx",
        f"https://www.ozon.ru/product/slug-{MAX_ARTICLE + 1}/",
        "https://www.wildberries.ru/catalog/1/detail.aspx\x00",
    ],
)
def test_parse_product_ref_rejects_unsupported_inputs(text: str) -> None:
    with pytest.raises(ProductRefParseError):
        parse_product_ref(text)


def test_parse_product_ref_accepts_max_bigint_article_in_a_link() -> None:
    url = f"https://www.wildberries.ru/catalog/{MAX_ARTICLE}/detail.aspx"
    assert parse_product_ref(url) == ProductRef(WB, 9_223_372_036_854_775_807)


def test_max_article_is_the_bigint_maximum() -> None:
    assert MAX_ARTICLE == 2**63 - 1


def test_input_of_2048_chars_is_parsed_and_2049_is_rejected_before_parsing() -> None:
    prefix = "https://www.wildberries.ru/catalog/5/detail.aspx?q="
    ok = prefix + "a" * (2048 - len(prefix))
    too_long = ok + "a"
    assert len(ok) == 2048
    assert parse_product_ref(ok) == ProductRef(WB, 5)
    with pytest.raises(ProductRefParseError):
        parse_product_ref(too_long)


def test_overlong_input_is_rejected_even_when_padded_with_whitespace() -> None:
    """The bound applies to the raw input: a valid article padded with spaces
    past the limit is still refused before ``strip``."""
    with pytest.raises(ProductRefParseError):
        parse_product_ref("5" + " " * 2048)


@pytest.mark.parametrize(
    "secret_input",
    [
        "https://evil.com/SECRET-MARKER-123",
        "SECRET-MARKER-123",
        "https://www.wildberries.ru/catalog/SECRET-MARKER-123/detail.aspx",
        "https://www.ozon.ru/product/slug-SECRET-MARKER-abc/",
        "SECRET MARKER 123",
        "https://www.wildberries.ru:SECRET-MARKER-123/",
    ],
)
def test_parse_error_message_does_not_contain_raw_input(secret_input: str) -> None:
    with pytest.raises(ProductRefParseError) as excinfo:
        parse_product_ref(secret_input)

    assert "SECRET" not in str(excinfo.value)
    assert "SECRET" not in excinfo.value.reason
    assert excinfo.value.__cause__ is None


def test_overlong_input_error_does_not_echo_the_input() -> None:
    with pytest.raises(ProductRefParseError) as excinfo:
        parse_product_ref("SECRET" * 500)

    assert "SECRET" not in str(excinfo.value)


def test_build_product_url_wildberries() -> None:
    assert (
        build_product_url(WB, 12345678)
        == "https://www.wildberries.ru/catalog/12345678/detail.aspx"
    )


def test_build_product_url_ozon() -> None:
    assert build_product_url(OZON, 1234567) == "https://www.ozon.ru/product/1234567/"


@pytest.mark.parametrize("marketplace", [WB, OZON])
@pytest.mark.parametrize("article", [1, 12345678, MAX_ARTICLE])
def test_build_then_parse_round_trips(marketplace: Marketplace, article: int) -> None:
    url = build_product_url(marketplace, article)

    assert parse_product_ref(url) == ProductRef(marketplace, article)


def test_build_product_url_has_no_default_branch_for_unknown_marketplace() -> None:
    with pytest.raises(AssertionError):
        build_product_url("amazon", 1)  # type: ignore[arg-type]


def test_product_ref_is_immutable() -> None:
    ref = ProductRef(WB, 1)
    with pytest.raises(AttributeError):
        ref.article = 2  # type: ignore[misc]
