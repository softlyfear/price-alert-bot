"""Inline keyboards of the Telegram bot.

``callback_data`` values are constants without identifiers: the dialog data
lives in the FSM storage, never in the callback payload.
"""

from typing import Final

from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup

from app.bot import texts

CB_ADD_CONFIRM: Final[str] = "add:confirm"
CB_ADD_CANCEL: Final[str] = "add:cancel"


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
