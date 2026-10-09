"""Single exit point for the outcome of a callback button press.

PROJECT.md section 2.10: a user action never has a silent outcome. The
decision "where to show the result" lives here only.
"""

from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardMarkup


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
