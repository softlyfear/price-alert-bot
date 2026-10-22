"""Parsing and building of marketplace product links (PROJECT.md section 2.2).

Pure string logic: no network access, so short links that need a redirect
resolution (e.g. Ozon `/t/...`) are rejected here by design.
"""

from dataclasses import dataclass
from typing import Final
from typing import assert_never
from urllib.parse import urlsplit

from app.domain.exceptions import MultipleProductRefsError
from app.domain.exceptions import ProductRefParseError
from app.models.enums import Marketplace

MAX_INPUT_LENGTH = 2048
MAX_ARTICLE = 9_223_372_036_854_775_807  # BIGINT of products.article

_WB_HOSTS = frozenset({"wildberries.ru", "www.wildberries.ru", "wb.ru", "www.wb.ru"})
_OZON_HOSTS = frozenset({"ozon.ru", "www.ozon.ru"})
_TOKEN_EDGE_CHARS = "«»\"'()[]<>,.;:!?…"

BARE_ARTICLE_MARKETPLACES: Final[tuple[Marketplace, ...]] = (
    Marketplace.wb,
    Marketplace.ozon,
)
"""Lookup order of a bare article: the one place that fixes it (R21)."""


@dataclass(frozen=True)
class ProductRef:
    """A marketplace and the article of a product on it."""

    marketplace: Marketplace
    article: int


@dataclass(frozen=True)
class BareArticle:
    """An article typed without a marketplace; the marketplace is searched."""

    article: int


type ProductQuery = ProductRef | BareArticle


def _article(token: str) -> int:
    if not (token.isascii() and token.isdigit()):
        raise ProductRefParseError("article is not a plain number")
    value = int(token)
    if not 0 < value <= MAX_ARTICLE:
        raise ProductRefParseError("article is out of range")
    return value


def _wb_article(path: str) -> int:
    parts = path.split("/")
    if len(parts) != 4 or parts[0] != "" or parts[1] != "catalog":
        raise ProductRefParseError("unsupported Wildberries path")
    if parts[3] != "detail.aspx":
        raise ProductRefParseError("unsupported Wildberries path")
    return _article(parts[2])


def _ozon_article(path: str) -> int:
    prefix = "/product/"
    if not path.startswith(prefix):
        raise ProductRefParseError("unsupported Ozon path")
    rest = path[len(prefix) :].removesuffix("/")
    if not rest or "/" in rest:
        raise ProductRefParseError("unsupported Ozon path")
    return _article(rest.rpartition("-")[2])


def parse_product_ref(text: str) -> ProductQuery:
    """Parse a product link into a `ProductRef`, bare digits into `BareArticle`.

    Bare digits carry no marketplace marker, so the marketplace stays open
    (`BARE_ARTICLE_MARKETPLACES`). The host is compared exactly after
    `urlsplit`, never by substring.
    """
    if len(text) > MAX_INPUT_LENGTH:
        raise ProductRefParseError("input is too long")
    value = text.strip()
    if not value:
        raise ProductRefParseError("input is empty")
    if value.isascii() and value.isdigit():
        return BareArticle(_article(value))
    return _parse_link(value)


def _parse_link(value: str) -> ProductRef:
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        raise ProductRefParseError("input contains whitespace")

    candidate = value if "://" in value else f"https://{value}"
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise ProductRefParseError("malformed URL") from None
    if parts.scheme not in {"http", "https"}:
        raise ProductRefParseError("unsupported URL scheme")
    if "@" in parts.netloc or port is not None or host is None:
        raise ProductRefParseError("unsupported URL authority")

    if host in _WB_HOSTS:
        return ProductRef(Marketplace.wb, _wb_article(parts.path))
    if host in _OZON_HOSTS:
        return ProductRef(Marketplace.ozon, _ozon_article(parts.path))
    raise ProductRefParseError("unsupported host")


def extract_product_ref(text: str) -> ProductQuery:
    """Find the single product link inside free text, e.g. a "Share" message.

    The whole text goes through `parse_product_ref` first, so anything accepted
    before is accepted identically. Otherwise the text is split on whitespace,
    edge punctuation is stripped and every token except a bare number is judged
    by the strict parser. A bare number inside a sentence is a price or a size,
    not an article. No match re-raises the original error; several different
    products raise `MultipleProductRefsError`.
    """
    try:
        return parse_product_ref(text)
    except ProductRefParseError as whole_text_error:
        if len(text) > MAX_INPUT_LENGTH:
            raise
        refs: set[ProductRef] = set()
        for raw in text.split():
            token = raw.strip(_TOKEN_EDGE_CHARS)
            if token.isascii() and token.isdigit():
                continue
            try:
                refs.add(_parse_link(token))
            except ProductRefParseError:
                continue
        if not refs:
            raise whole_text_error from None
        if len(refs) > 1:
            raise MultipleProductRefsError(len(refs)) from None
        return next(iter(refs))


def build_product_url(marketplace: Marketplace, article: int) -> str:
    """Build the canonical product URL; the only place that does it."""
    match marketplace:
        case Marketplace.wb:
            return f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        case Marketplace.ozon:
            return f"https://www.ozon.ru/product/{article}/"
        case _:
            assert_never(marketplace)
