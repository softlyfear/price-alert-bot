"""Tests for ``edit_callback_message``, the in-place exit point (PAB-092 AC7).

The function is exercised through the real dispatcher with the ``menu:start``
button ("Main menu"), which shows ``START_TEXT`` with the main menu keyboard
and does nothing else. The fake Bot API session raises on demand, so every
branch of the matrix is observed from the outside: which requests went to
Telegram and how many times the callback was answered.
"""

from __future__ import annotations

from typing import Any
from typing import cast

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram import F
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.keyboards import main_menu_keyboard
from app.handlers.callback_reply import edit_callback_message
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_callback_update

_CHAT = 9_999
_USER = 4_242
_MENU = ["menu:add", "menu:list", "menu:help"]


class _Session:
    """Bot plus the error the fake API raises for ``editMessageText``."""

    def __init__(self, edit_error: Exception | None = None) -> None:
        self.edit_error = edit_error
        self.bot: Bot = build_fake_bot(responder=self._respond)

    async def _respond(self, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, EditMessageText) and self.edit_error is not None:
            raise self.edit_error
        return True

    @property
    def requests(self) -> list[TelegramMethod[Any]]:
        return cast(Any, self.bot.session).requests  # type: ignore[no-any-return]

    def of[T: TelegramMethod[Any]](self, kind: type[T]) -> list[T]:
        return [r for r in self.requests if isinstance(r, kind)]


def _edit_error(error_type: type[Exception]) -> Exception:
    method = EditMessageText(chat_id=_CHAT, message_id=5, text="x")
    if error_type is TelegramBadRequest:
        return TelegramBadRequest(method=method, message="Bad Request: boom")
    return TelegramNetworkError(method=method, message="boom")


async def _press(dispatcher: Dispatcher, session: _Session, **kwargs: Any) -> None:
    await dispatcher.feed_update(
        session.bot,
        make_callback_update("menu:start", chat_id=_CHAT, user_id=_USER, **kwargs),
    )


def _menu_callbacks(request: SendMessage | EditMessageText) -> list[str | None]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_accessible_message_with_other_content_is_edited_in_place_once(
    real_dispatcher: Dispatcher,
) -> None:
    session = _Session()

    await _press(real_dispatcher, session, message_text="old text")

    edits = session.of(EditMessageText)
    assert [(e.chat_id, e.message_id, e.text, e.parse_mode) for e in edits] == [
        (_CHAT, 5, texts.START_TEXT, None)
    ]
    assert _menu_callbacks(edits[0]) == _MENU
    assert session.of(SendMessage) == []
    assert [(a.text, a.show_alert) for a in session.of(AnswerCallbackQuery)] == [
        (None, None)
    ]


@pytest.mark.asyncio
async def test_same_text_and_keyboard_neither_edits_nor_sends(
    real_dispatcher: Dispatcher,
) -> None:
    session = _Session()

    await _press(
        real_dispatcher,
        session,
        message_text=texts.START_TEXT,
        message_markup=main_menu_keyboard(),
    )

    assert session.of(EditMessageText) == []
    assert session.of(SendMessage) == []
    assert len(session.of(AnswerCallbackQuery)) == 1


@pytest.mark.asyncio
async def test_same_text_with_another_keyboard_is_still_edited(
    real_dispatcher: Dispatcher,
) -> None:
    other = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x")]]
    )
    session = _Session()

    await _press(
        real_dispatcher,
        session,
        message_text=texts.START_TEXT,
        message_markup=other,
    )

    assert len(session.of(EditMessageText)) == 1
    assert len(session.of(AnswerCallbackQuery)) == 1


@pytest.mark.asyncio
async def test_other_text_with_the_same_keyboard_is_still_edited(
    real_dispatcher: Dispatcher,
) -> None:
    session = _Session()

    await _press(
        real_dispatcher,
        session,
        message_text="old text",
        message_markup=main_menu_keyboard(),
    )

    assert len(session.of(EditMessageText)) == 1
    assert len(session.of(AnswerCallbackQuery)) == 1


@pytest.mark.asyncio
async def test_rejected_edit_falls_back_to_one_new_plain_message(
    real_dispatcher: Dispatcher,
) -> None:
    session = _Session(_edit_error(TelegramBadRequest))

    await _press(real_dispatcher, session)

    assert len(session.of(EditMessageText)) == 1
    sent = session.of(SendMessage)
    assert [(m.chat_id, m.text, m.parse_mode) for m in sent] == [
        (_CHAT, texts.START_TEXT, None)
    ]
    assert _menu_callbacks(sent[0]) == _MENU
    assert len(session.of(AnswerCallbackQuery)) == 1


@pytest.mark.asyncio
async def test_inaccessible_message_gets_one_new_plain_message_and_no_edit(
    real_dispatcher: Dispatcher,
) -> None:
    session = _Session()

    await _press(real_dispatcher, session, accessible=False)

    assert session.of(EditMessageText) == []
    sent = session.of(SendMessage)
    assert [(m.chat_id, m.text, m.parse_mode) for m in sent] == [
        (_CHAT, texts.START_TEXT, None)
    ]
    assert _menu_callbacks(sent[0]) == _MENU
    assert len(session.of(AnswerCallbackQuery)) == 1


@pytest.mark.asyncio
async def test_missing_message_gives_one_popup_and_nothing_else(
    real_dispatcher: Dispatcher,
) -> None:
    session = _Session()

    await _press(real_dispatcher, session, with_message=False)

    assert [type(r) for r in session.requests] == [AnswerCallbackQuery]
    answer = session.of(AnswerCallbackQuery)[0]
    assert (answer.text, answer.show_alert) == (texts.MENU_STALE_ALERT_TEXT, True)


@pytest.mark.asyncio
async def test_other_bot_api_error_goes_to_the_errors_handler_not_a_new_message(
    real_dispatcher: Dispatcher,
) -> None:
    session = _Session(_edit_error(TelegramNetworkError))

    await _press(real_dispatcher, session)

    assert len(session.of(EditMessageText)) == 1
    sent = session.of(SendMessage)
    assert [m.text for m in sent] == [texts.UNEXPECTED_ERROR_TEXT]
    assert _menu_callbacks(sent[0]) == _MENU
    assert len(session.of(AnswerCallbackQuery)) == 1


@pytest.mark.asyncio
async def test_missing_message_without_alert_text_shows_the_screen_text_as_popup() -> (
    None
):
    router = Router(name="probe")

    @router.callback_query(F.data == "probe")
    async def probe(callback: CallbackQuery) -> None:
        await edit_callback_message(callback, "screen text")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    session = _Session()

    await dispatcher.feed_update(
        session.bot,
        make_callback_update("probe", chat_id=_CHAT, user_id=_USER, with_message=False),
    )

    answers = session.of(AnswerCallbackQuery)
    assert [(a.text, a.show_alert) for a in answers] == [("screen text", True)]
