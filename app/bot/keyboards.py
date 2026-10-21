"""Inline keyboards of the Telegram bot.

The /add dialog's ``callback_data`` values are constants without
identifiers: the dialog data lives in the FSM storage, never in the callback
payload. The /list keyboards carry a single database id in a typed
``CallbackData`` (a callback payload is user input: ids are range-checked).
"""

from collections import Counter
from collections.abc import Sequence
from typing import Final

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from pydantic import Field

from app.bot import texts
from app.domain.links import MAX_ARTICLE
from app.domain.money import format_price
from app.models.alert import Alert
from app.models.enums import Marketplace
from app.models.product import Product

CB_ADD_CONFIRM: Final[str] = "add:confirm"
CB_ADD_CANCEL: Final[str] = "add:cancel"

CB_MENU_PREFIX: Final[str] = "menu:"
CB_MENU_ADD: Final[str] = "menu:add"
CB_MENU_LIST: Final[str] = "menu:list"
CB_MENU_HELP: Final[str] = "menu:help"
CB_MENU_START: Final[str] = "menu:start"
CB_MENU_ALL: Final[frozenset[str]] = frozenset(
    {CB_MENU_ADD, CB_MENU_LIST, CB_MENU_HELP, CB_MENU_START}
)

# Primary keys are int4 (PostgreSQL ``Integer``).
_MAX_ID: Final[int] = 2_147_483_647
MAX_LIST_BUTTONS: Final[int] = 50
_MAX_NAME_IN_BUTTON: Final[int] = 40
CB_LIST_PREFIXES: Final[tuple[str, ...]] = ("pcard", "adel", "pdel")


class ProductCardCb(CallbackData, prefix="pcard"):
    """Open the card of one product."""

    product_id: int = Field(ge=1, le=_MAX_ID)


class AlertRemoveCb(CallbackData, prefix="adel"):
    """Remove one threshold; ``product_id`` is the card to redraw."""

    alert_id: int = Field(ge=1, le=_MAX_ID)
    product_id: int = Field(ge=1, le=_MAX_ID)


class ProductRemoveCb(CallbackData, prefix="pdel"):
    """Stop tracking one product."""

    product_id: int = Field(ge=1, le=_MAX_ID)


class AddRefCb(CallbackData, prefix="addref"):
    """Start the /add link step with an already parsed product reference."""

    marketplace: Marketplace
    article: int = Field(ge=1, le=MAX_ARTICLE)


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu: add a product, list, help."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(texts.MENU_ADD_BUTTON_TEXT, CB_MENU_ADD)],
            [
                _button(texts.MENU_LIST_BUTTON_TEXT, CB_MENU_LIST),
                _button(texts.MENU_HELP_BUTTON_TEXT, CB_MENU_HELP),
            ],
        ]
    )


def add_ref_keyboard(marketplace: Marketplace, article: int) -> InlineKeyboardMarkup:
    """Offer to add the recognised product, or go to the main menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    texts.ADD_REF_BUTTON_TEXT,
                    AddRefCb(marketplace=marketplace, article=article).pack(),
                )
            ],
            [_button(texts.MENU_MAIN_BUTTON_TEXT, CB_MENU_START)],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Keyboard with a single cancel button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.CANCEL_BUTTON_TEXT, callback_data=CB_ADD_CANCEL
                )
            ]
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Keyboard of the confirmation step."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.CONFIRM_BUTTON_TEXT, callback_data=CB_ADD_CONFIRM
                ),
                InlineKeyboardButton(
                    text=texts.CANCEL_BUTTON_TEXT, callback_data=CB_ADD_CANCEL
                ),
            ]
        ]
    )


def _short_name(name: str) -> str:
    """Collapse whitespace and cut the name to fit a button label."""
    flat = " ".join(name.split())
    if len(flat) > _MAX_NAME_IN_BUTTON:
        return flat[: _MAX_NAME_IN_BUTTON - 1] + "…"
    return flat


def product_list_keyboard(products: Sequence[Product]) -> InlineKeyboardMarkup:
    """One button per product (at most ``MAX_LIST_BUTTONS``) plus a nav row."""
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{_short_name(product.product_name)} — "
                    f"{format_price(product.current_price)}"
                ),
                callback_data=ProductCardCb(product_id=product.id).pack(),
            )
        ]
        for product in products[:MAX_LIST_BUTTONS]
    ]
    rows.append(
        [
            _button(texts.MENU_ADD_BUTTON_TEXT, CB_MENU_ADD),
            _button(texts.MENU_MAIN_BUTTON_TEXT, CB_MENU_START),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nav_row() -> list[InlineKeyboardButton]:
    """Navigation row of a card-like screen: back to the list, main menu."""
    return [
        _button(texts.LIST_BACK_BUTTON_TEXT, CB_MENU_LIST),
        _button(texts.MENU_MAIN_BUTTON_TEXT, CB_MENU_START),
    ]


def nav_keyboard() -> InlineKeyboardMarkup:
    """Keyboard with the navigation row only (no dead-end screens)."""
    return InlineKeyboardMarkup(inline_keyboard=[nav_row()])


def product_card_keyboard(
    product_id: int, alerts: Sequence[Alert]
) -> InlineKeyboardMarkup:
    """Remove-threshold button per alert, stop-tracking button, nav row."""
    amount_counts = Counter(alert.target_price for alert in alerts)
    rows = [
        [
            InlineKeyboardButton(
                text=texts.remove_alert_button_text(
                    alert.direction,
                    alert.target_price,
                    with_arrow=amount_counts[alert.target_price] > 1,
                ),
                callback_data=AlertRemoveCb(
                    alert_id=alert.id, product_id=product_id
                ).pack(),
            )
        ]
        for alert in alerts
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=texts.STOP_TRACKING_BUTTON_TEXT,
                callback_data=ProductRemoveCb(product_id=product_id).pack(),
            )
        ]
    )
    rows.append(nav_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)
