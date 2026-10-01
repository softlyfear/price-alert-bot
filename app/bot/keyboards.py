"""Inline keyboards of the Telegram bot.

The /add dialog's ``callback_data`` values are constants without
identifiers: the dialog data lives in the FSM storage, never in the callback
payload. The /list keyboards carry a single database id in a typed
``CallbackData`` (a callback payload is user input: ids are range-checked).
"""

from collections.abc import Sequence
from typing import Final

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from pydantic import Field

from app.bot import texts
from app.domain.money import format_price
from app.models.alert import Alert
from app.models.product import Product

CB_ADD_CONFIRM: Final[str] = "add:confirm"
CB_ADD_CANCEL: Final[str] = "add:cancel"

# Primary keys are int4 (PostgreSQL ``Integer``).
_MAX_ID: Final[int] = 2_147_483_647
MAX_LIST_BUTTONS: Final[int] = 50
_MAX_NAME_IN_BUTTON: Final[int] = 40
CB_LIST_PREFIXES: Final[tuple[str, ...]] = ("pcard", "adel", "pdel")


class ProductCardCb(CallbackData, prefix="pcard"):
    """Open the card of one product."""

    product_id: int = Field(ge=1, le=_MAX_ID)


class AlertRemoveCb(CallbackData, prefix="adel"):
    """Remove one threshold."""

    alert_id: int = Field(ge=1, le=_MAX_ID)


class ProductRemoveCb(CallbackData, prefix="pdel"):
    """Stop tracking one product."""

    product_id: int = Field(ge=1, le=_MAX_ID)


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
    """One button per product, at most ``MAX_LIST_BUTTONS``."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
    )


def product_card_keyboard(
    product_id: int, alerts: Sequence[Alert]
) -> InlineKeyboardMarkup:
    """Remove-threshold button per alert plus a stop-tracking button."""
    rows = [
        [
            InlineKeyboardButton(
                text=texts.remove_alert_button_text(
                    alert.direction, alert.target_price
                ),
                callback_data=AlertRemoveCb(alert_id=alert.id).pack(),
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
    return InlineKeyboardMarkup(inline_keyboard=rows)
