"""Schemas for the Ozon `entrypoint-api` response.

Contract established by a fact, not a live call: the sole reference response
is the customer-supplied capture for article `3593896354`, dated 2026-10-09
(`PROJECT.md` section 2.11). The response is not reproducible from the
current working environment (anti-bot).

The response is double-encoded: `widgetStates` maps a widget key
(`<component>-<id>-<container>-<page>`) to a *string* of JSON, which must be
parsed a second time. This module therefore describes two levels:
`OzonEntrypointResponse` for the body, and one model per widget state for
the strings inside it (`Model.model_validate_json(state_string)`).

Only the fields `OzonClient` reads are modeled:

- `OzonProductHeadingState.title` - the product name.
- `OzonPriceState.isAvailable` / `price` - availability and the price
  without the Ozon card. `cardPrice` (a payment-condition, i.e. personal,
  price) and `originalPrice` (crossed out) are deliberately not read
  (`PROJECT.md` sections 2.9, 2.11).
- `OzonDetailSkuState.copyText` - the article the page shows; the client
  compares it with the requested article.

Widget selection (by the key segment before the first `-`, exactly one
entry) and product identity are policy decisions that belong to the client;
this module only describes the shape and the price format.

All models use `extra="ignore"` (the real response has 19 top-level keys and
81 widgets, of which three are read) and `frozen=True`.
"""

import re
from typing import Annotated

from pydantic import BaseModel
from pydantic import BeforeValidator
from pydantic import ConfigDict
from pydantic import Field

# Upper bound on one widget-state string and on the widget count: the client
# already caps the body at 4 MiB, this keeps the schema safe on its own.
_MAX_STATE_CHARS = 4 * 1024 * 1024
_MAX_WIDGETS = 2000
_MAX_WIDGET_KEY_CHARS = 256
# Generous caps for the raw strings that are read, so that an inflated value
# cannot reach downstream code. `title` is bounded to 150 later, by
# `MarketplaceProductData`; this is only the DoS guard.
_MAX_TITLE_CHARS = 1000
_MAX_COPY_TEXT_CHARS = 64
# A longer price string is rejected before the regex runs.
_MAX_PRICE_CHARS = 32

_THIN_SPACE = "\u2009"
_RUBLE_SIGN = "\u20bd"
# The only observed form: 1-3 digits without a leading zero, groups of exactly
# three digits separated by U+2009, then U+2009 and U+20BD. Explicit `[0-9]`
# (not `\d`, not `str.isdigit`) so that non-ASCII digits are rejected.
_PRICE_RE = re.compile(
    rf"[1-9][0-9]{{0,2}}(?:{_THIN_SPACE}[0-9]{{3}})*{_THIN_SPACE}{_RUBLE_SIGN}"
)


def parse_rubles_to_kopecks(value: object) -> int:
    """Convert an Ozon price string such as `495 ₽` to integer kopecks.

    The separators are U+2009 (THIN SPACE) and the sign is U+20BD. Any other
    form raises `ValueError`, which pydantic turns into a validation error.
    Pure function; runs in `O(len(value))`.
    """
    if not isinstance(value, str):
        raise ValueError("price must be a string")
    if len(value) > _MAX_PRICE_CHARS or _PRICE_RE.fullmatch(value) is None:
        raise ValueError("unrecognized price format")
    digits = value.removesuffix(f"{_THIN_SPACE}{_RUBLE_SIGN}").replace(_THIN_SPACE, "")
    return int(digits) * 100


# Rubles string -> integer kopecks; the single point of truth for the format.
OzonPriceKopecks = Annotated[int, BeforeValidator(parse_rubles_to_kopecks)]


class OzonProductHeadingState(BaseModel):
    """State of the `webProductHeading` widget: the product name."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    title: Annotated[str, Field(strict=True, max_length=_MAX_TITLE_CHARS)]


class OzonPriceState(BaseModel):
    """State of the `webPrice` widget.

    `price` is the price without the Ozon card, in kopecks after validation.
    `isAvailable` is a strict `bool`: `false` was never observed, so what it
    means is a client policy decision.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    is_available: Annotated[bool, Field(strict=True, alias="isAvailable")]
    price: OzonPriceKopecks


class OzonDetailSkuState(BaseModel):
    """State of the `webDetailSKU` widget: the article shown on the page."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    copy_text: Annotated[
        str,
        Field(strict=True, max_length=_MAX_COPY_TEXT_CHARS, alias="copyText"),
    ]


class OzonEntrypointResponse(BaseModel):
    """Top level of the response; `widgetStates` values are JSON strings."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    widget_states: Annotated[
        dict[
            Annotated[str, Field(max_length=_MAX_WIDGET_KEY_CHARS)],
            Annotated[str, Field(strict=True, max_length=_MAX_STATE_CHARS)],
        ],
        Field(alias="widgetStates", max_length=_MAX_WIDGETS),
    ]
