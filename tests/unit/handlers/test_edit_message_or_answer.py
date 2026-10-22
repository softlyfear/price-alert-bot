"""``edit_message_or_answer``: the second screen of a long action (PAB-100 AC8).

The callback is already answered when this runs, so the function only edits the
message and, if Telegram refuses the edit, sends the same screen as a new
message. The ``Message`` is a double whose ``edit_text`` and ``answer`` are
recorded; the refusal is the real ``TelegramBadRequest``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message

from app.handlers.callback_reply import edit_message_or_answer

if TYPE_CHECKING:
    from loguru import Record

_MARKUP = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Кнопка", callback_data="x")]]
)


def _message(
    *, edit_error: Exception | None = None, answer_error: Exception | None = None
) -> MagicMock:
    message = MagicMock(spec=Message)
    message.edit_text = AsyncMock(side_effect=edit_error)
    message.answer = AsyncMock(side_effect=answer_error)
    return message


@pytest.mark.asyncio
async def test_edits_the_message_in_place_as_plain_text() -> None:
    message = _message()

    await edit_message_or_answer(message, "Экран", reply_markup=_MARKUP)

    message.edit_text.assert_awaited_once_with(
        "Экран", reply_markup=_MARKUP, parse_mode=None
    )
    message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_without_a_keyboard_passes_none() -> None:
    message = _message()

    await edit_message_or_answer(message, "Экран")

    message.edit_text.assert_awaited_once_with(
        "Экран", reply_markup=None, parse_mode=None
    )


@pytest.mark.asyncio
async def test_a_refused_edit_sends_the_same_screen_as_a_new_message() -> None:
    message = _message(
        edit_error=TelegramBadRequest(method=MagicMock(), message="can't be edited")
    )

    await edit_message_or_answer(message, "Экран", reply_markup=_MARKUP)

    message.answer.assert_awaited_once_with(
        "Экран", reply_markup=_MARKUP, parse_mode=None
    )


@pytest.mark.asyncio
async def test_a_refused_edit_is_logged_once_at_warning_with_the_error_type_only(
    log_records: list[Record],
) -> None:
    message = _message(
        edit_error=TelegramBadRequest(method=MagicMock(), message="secret-ish text")
    )

    await edit_message_or_answer(message, "Экран")

    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0]["extra"] == {"error_type": "TelegramBadRequest"}
    assert "secret-ish text" not in warnings[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TelegramNetworkError(method=MagicMock(), message="down"),
        TelegramForbiddenError(method=MagicMock(), message="blocked by the user"),
    ],
    ids=["network", "forbidden"],
)
async def test_other_bot_api_errors_are_not_swallowed_and_send_nothing(
    error: Exception,
) -> None:
    message = _message(edit_error=error)

    with pytest.raises(type(error)):
        await edit_message_or_answer(message, "Экран")

    message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_a_failing_fallback_message_reaches_the_caller() -> None:
    message = _message(
        edit_error=TelegramBadRequest(method=MagicMock(), message="can't be edited"),
        answer_error=TelegramNetworkError(method=MagicMock(), message="down"),
    )

    with pytest.raises(TelegramNetworkError):
        await edit_message_or_answer(message, "Экран")
