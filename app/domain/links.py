"""Parsing and building of marketplace product links (PROJECT.md section 2.2).

Pure string logic: no network access, so short links that need a redirect
resolution (e.g. Ozon `/t/...`) are rejected here by design.
"""

from dataclasses import dataclass
from typing import assert_never
from urllib.parse import urlsplit

from app.domain.exceptions import ProductRefParseError
from app.models.enums import Marketplace

MAX_INPUT_LENGTH = 2048
MAX_ARTICLE = 9_223_372_036_854_775_807  # BIGINT of products.article

_WB_HOSTS = frozenset({"wildberries.ru", "www.wildberries.ru", "wb.ru", "www.wb.ru"})
_OZON_HOSTS = frozenset({"ozon.ru", "www.ozon.ru"})


@dataclass(frozen=True)
class ProductRef:
    """A marketplace and the article of a product on it."""

    marketplace: Marketplace
    article: int


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


def parse_product_ref(text: str) -> ProductRef:
    """Parse a product link or a bare article into a `ProductRef`.

    Bare digits mean Wildberries (a number carries no marketplace marker).
    The host is compared exactly after `urlsplit`, never by substring.
    """
    if len(text) > MAX_INPUT_LENGTH:
        raise ProductRefParseError("input is too long")
    value = text.strip()
    if not value:
        raise ProductRefParseError("input is empty")
    if value.isascii() and value.isdigit():
        return ProductRef(Marketplace.wb, _article(value))
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


def build_product_url(marketplace: Marketplace, article: int) -> str:
    """Build the canonical product URL; the only place that does it."""
    match marketplace:
        case Marketplace.wb:
            return f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        case Marketplace.ozon:
            return f"https://www.ozon.ru/product/{article}/"
        case _:
            assert_never(marketplace)
