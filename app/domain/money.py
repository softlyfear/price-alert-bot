"""Single source of truth for the price disclaimer text (PROJECT.md §2.9).

The bot compares the tracked price against the threshold using the public
price returned by a marketplace API, which excludes personal discounts
(wallet balances, marketplace cards, loyalty points, etc.). This module
holds the two approved wordings of the disclaimer that must accompany any
price shown to the user, so both the bot layer (`app/bot/**`,
`app/handlers/**`) and `NotificationService` read the same text instead of
keeping independent copies that could drift apart.

Money parsing (rubles to integer kopecks) and price formatting live here too.
"""

import re
from typing import Final

from app.domain.exceptions import InvalidPriceInputError

# int4 limit of the price columns; mirrors the bound in MarketplaceProductData.
MAX_PRICE_KOPECKS: Final[int] = 2_147_483_647

_MAX_INPUT_LENGTH: Final[int] = 32
_NBSP: Final[str] = "\u00a0"
_PRICE_RE: Final[re.Pattern[str]] = re.compile(
    r"[0-9]+(?:[ \u00a0][0-9]+)*(?:[.,][0-9]{1,2})?"
)

PRICE_DISCLAIMER_SHORT: Final[str] = "Цена без учёта персональных скидок"

PRICE_DISCLAIMER_FULL: Final[str] = (
    "Бот показывает цену без учёта персональных скидок: кошелька или карты "
    "маркетплейса, баллов и т. п. На сайте цена для вас может быть ниже."
)


def rubles_to_kopecks(text: str) -> int:
    """Convert user-entered rubles to integer kopecks without floats."""
    if len(text) > _MAX_INPUT_LENGTH:
        raise InvalidPriceInputError("input is too long")
    value = text.strip()
    if _PRICE_RE.fullmatch(value) is None:
        raise InvalidPriceInputError("unsupported price format")
    whole, _, fraction = value.replace(",", ".").partition(".")
    digits = whole.replace(" ", "").replace(_NBSP, "")
    kopecks = int(digits) * 100 + int(fraction.ljust(2, "0") or "0")
    if kopecks <= 0:
        raise InvalidPriceInputError("price must be positive")
    if kopecks > MAX_PRICE_KOPECKS:
        raise InvalidPriceInputError("price is too large")
    return kopecks


def format_price(kopecks: int) -> str:
    """Format kopecks as `1 990 ₽` / `1 990,50 ₽` with U+00A0 separators."""
    if kopecks < 0:
        raise ValueError("kopecks must be non-negative")
    rubles, rest = divmod(kopecks, 100)
    grouped = f"{rubles:,}".replace(",", _NBSP)
    fraction = f",{rest:02d}" if rest else ""
    return f"{grouped}{fraction}{_NBSP}₽"
