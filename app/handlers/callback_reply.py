"""Single exit point for the outcome of a callback button press.

PROJECT.md section 2.10: a user action never has a silent outcome. The
decision "where to show the result" lives here only.
"""

from aiogram.exceptions import TelegramAPIError
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message
from loguru import logger


async def reply_to_callback(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    alert_text: str | None = None,
) -> None:
    """Answer the callback exactly once and deliver the outcome to the user.

    A message that still has a chat (accessible or not) gets a new message in
    that chat; with no message at all the outcome is shown as a popup alert
    (``alert_text`` if given, else ``text``). A popup is limited to 200
    characters by the Bot API, so callers must pass only short texts there.
    """
    message = callback.message
    if message is None:
        await callback.answer(alert_text or text, show_alert=True)
        return
    await callback.answer()
    await message.answer(text, reply_markup=reply_markup, parse_mode=None)


def _same_markup(
    current: InlineKeyboardMarkup | None, new: InlineKeyboardMarkup | None
) -> bool:
    """Compare keyboards by content.

    Pydantic ``==`` also compares the private ``_bot`` field, which is set on a
    parsed Telegram message and ``None`` on a freshly built keyboard.
    """
    return current is new or (
        current is not None
        and new is not None
        and current.model_dump(exclude_none=True) == new.model_dump(exclude_none=True)
    )


async def _answer_after_delivery(
    callback: CallbackQuery, answer_text: str | None
) -> None:
    """Answer the callback once the screen is already shown.

    The answer is cosmetic by then; letting its failure propagate would roll
    back the database transaction of an outcome the user already sees.
    """
    try:
        await callback.answer(answer_text)
    except TelegramAPIError as exc:
        logger.warning("Callback answer failed after delivery: {}", type(exc).__name__)


async def edit_callback_message(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    alert_text: str | None = None,
    answer_text: str | None = None,
) -> None:
    """Show a navigation screen by editing the pressed message in place.

    Identical content is neither edited nor duplicated (compared before the
    edit, not by parsing the "message is not modified" error). A refused edit
    (``TelegramBadRequest``) or an inaccessible message falls back to a new
    message in the chat; with no message a popup is shown. The callback is
    answered exactly once, with ``answer_text`` as a short toast if given. A
    failed answer after the screen is delivered is logged and swallowed; other
    Bot API errors before delivery propagate to the ``errors`` handler.
    """
    message = callback.message
    if message is None:
        await callback.answer(alert_text or text, show_alert=True)
        return
    if isinstance(message, Message):
        if message.text == text and _same_markup(message.reply_markup, reply_markup):
            await _answer_after_delivery(callback, answer_text)
            return
        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode=None)
        except TelegramBadRequest as exc:
            logger.warning(
                "Edit refused, sending a new message: {}", type(exc).__name__
            )
        else:
            await _answer_after_delivery(callback, answer_text)
            return
    await message.answer(text, reply_markup=reply_markup, parse_mode=None)
    await _answer_after_delivery(callback, answer_text)
