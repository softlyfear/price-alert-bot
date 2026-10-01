"""Tests for app.handlers.errors, the catch-all handler (PAB-072 AC6, AC7 (e)).

Everything goes through ``Dispatcher.feed_update`` on the real
``create_dispatcher()`` output, so the claim the design rests on is proven
rather than assumed: ``aiogram``'s ``ErrorsMiddleware`` wraps
``DbSessionMiddleware``, hence a failing ``COMMIT`` reaches the handler.
A tiny extra router with deliberately failing handlers is the only
test-owned production stand-in.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram import F
from aiogram import Router
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import SendMessage
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery
from aiogram.types import ChosenInlineResult
from aiogram.types import Message
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import texts
from app.bot.setup import create_dispatcher
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_callback_update
from tests.unit.conftest import make_message_update

if TYPE_CHECKING:
    from loguru import Record

_CHAT = 9_999
_USER = 4_242
_USER_TEXT_MARKER = "USER-TEXT-MARKER-7731"
_BOT_TOKEN = "1:test-token"


class _Boom(Exception):
    """Distinct exception type so a test can tell it from any other failure."""


def _failing_router() -> Router:
    router = Router(name="failing")

    @router.message(F.text.startswith("boom"))
    async def on_message(message: Message) -> None:
        raise _Boom("handler failed")

    @router.message(F.text == "cancel-me")
    async def on_cancel(message: Message) -> None:
        raise asyncio.CancelledError

    @router.callback_query(F.data == "boom")
    async def on_callback(callback: CallbackQuery) -> None:
        raise _Boom("callback failed")

    @router.chosen_inline_result()
    async def on_chosen(result: ChosenInlineResult) -> None:
        raise _Boom("no chat here")

    return router


def _dispatcher() -> Dispatcher:
    dispatcher = create_dispatcher(
        MemoryStorage(),
        async_sessionmaker(class_=AsyncSession),
        httpx.AsyncClient(),
        50,
    )
    dispatcher.include_router(_failing_router())
    return dispatcher


def _sent(bot: Bot) -> list[SendMessage]:
    return [r for r in bot.session.requests if isinstance(r, SendMessage)]  # type: ignore[attr-defined]


def _answers(bot: Bot) -> list[AnswerCallbackQuery]:
    return [
        r
        for r in bot.session.requests  # type: ignore[attr-defined]
        if isinstance(r, AnswerCallbackQuery)
    ]


def _errors(records: list[Record]) -> list[Record]:
    return [r for r in records if r["level"].name == "ERROR"]


def _warnings(records: list[Record]) -> list[Record]:
    return [r for r in records if r["level"].name == "WARNING"]


def _boom_message(update_id: int = 41) -> Update:
    return make_message_update(
        text="boom", update_id=update_id, chat_id=_CHAT, user_id=_USER
    )


@pytest.mark.asyncio
async def test_handler_failure_is_logged_once_at_error_with_update_context(
    log_records: list[Record],
) -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()

    await dispatcher.feed_update(bot, _boom_message(update_id=41))

    errors = _errors(log_records)
    assert len(errors) == 1
    assert errors[0]["extra"]["update_id"] == 41
    assert errors[0]["extra"]["update_type"] == "message"
    exception = errors[0]["exception"]
    assert exception is not None
    assert isinstance(exception.value, _Boom)


@pytest.mark.asyncio
async def test_handler_failure_tells_the_user_it_may_not_have_worked(
    log_records: list[Record],
) -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()

    await dispatcher.feed_update(bot, _boom_message())

    sent = _sent(bot)
    assert [m.text for m in sent] == [texts.UNEXPECTED_ERROR_TEXT]
    assert sent[0].chat_id == _CHAT
    assert sent[0].parse_mode is None


@pytest.mark.asyncio
async def test_log_record_carries_neither_user_text_nor_the_bot_token(
    log_records: list[Record],
) -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot(token=_BOT_TOKEN)
    update = make_message_update(
        text=f"boom {_USER_TEXT_MARKER}", update_id=3, chat_id=_CHAT, user_id=_USER
    )

    await dispatcher.feed_update(bot, update)

    error = _errors(log_records)[0]
    rendered = f"{error['message']} {error['extra']}"
    assert _USER_TEXT_MARKER not in rendered
    assert _BOT_TOKEN not in rendered


@pytest.mark.asyncio
async def test_commit_failure_after_a_successful_handler_reaches_the_error_handler(
    log_records: list[Record],
) -> None:
    """AC7 (e): proves ``ErrorsMiddleware`` wraps ``DbSessionMiddleware``."""

    class _CommitFails(AsyncSession):
        async def commit(self) -> None:
            raise _Boom("COMMIT failed")

    dispatcher = create_dispatcher(
        MemoryStorage(),
        async_sessionmaker(class_=_CommitFails),
        httpx.AsyncClient(),
        50,
    )
    bot = build_fake_bot()

    await dispatcher.feed_update(
        bot,
        make_message_update(text="/start", update_id=77, chat_id=_CHAT, user_id=_USER),
    )

    # The /start handler ran and answered, then COMMIT failed and was handled.
    assert [m.text for m in _sent(bot)] == [
        texts.START_TEXT,
        texts.UNEXPECTED_ERROR_TEXT,
    ]
    errors = _errors(log_records)
    assert len(errors) == 1
    assert errors[0]["extra"]["update_id"] == 77
    exception = errors[0]["exception"]
    assert exception is not None
    assert isinstance(exception.value, _Boom)
    assert str(exception.value) == "COMMIT failed"


@pytest.mark.asyncio
async def test_callback_failure_answers_the_callback_and_the_chat(
    log_records: list[Record],
) -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()

    await dispatcher.feed_update(
        bot, make_callback_update("boom", update_id=8, chat_id=_CHAT, user_id=_USER)
    )

    assert len(_answers(bot)) == 1
    assert [m.text for m in _sent(bot)] == [texts.UNEXPECTED_ERROR_TEXT]
    assert _errors(log_records)[0]["extra"]["update_type"] == "callback_query"


@pytest.mark.asyncio
async def test_callback_on_an_inaccessible_message_is_answered_without_a_chat_reply(
    log_records: list[Record],
) -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()

    await dispatcher.feed_update(
        bot,
        make_callback_update("boom", chat_id=_CHAT, user_id=_USER, accessible=False),
    )

    assert len(_answers(bot)) == 1
    assert _sent(bot) == []
    assert len(_errors(log_records)) == 1


@pytest.mark.asyncio
async def test_update_without_a_chat_is_only_logged(log_records: list[Record]) -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()
    update = Update.model_validate(
        {
            "update_id": 12,
            "chosen_inline_result": {
                "result_id": "r",
                "from": {"id": _USER, "is_bot": False, "first_name": "T"},
                "query": "q",
            },
        }
    )

    await dispatcher.feed_update(bot, update)

    assert bot.session.requests == []  # type: ignore[attr-defined]
    errors = _errors(log_records)
    assert len(errors) == 1
    assert errors[0]["extra"]["update_type"] == "chosen_inline_result"


@pytest.mark.asyncio
async def test_failed_error_reply_is_a_warning_and_is_not_propagated(
    log_records: list[Record],
) -> None:
    async def responder(method: TelegramMethod[Any]) -> Any:
        raise RuntimeError("telegram down")

    dispatcher, bot = _dispatcher(), build_fake_bot(responder=responder)

    await dispatcher.feed_update(bot, _boom_message(update_id=5))

    assert len(_errors(log_records)) == 1  # the original failure is still recorded
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert "telegram down" in warnings[0]["message"]
    assert warnings[0]["extra"]["update_id"] == 5


def _responder_failing_on(*failing: type[TelegramMethod[Any]]) -> Any:
    async def responder(method: TelegramMethod[Any]) -> Any:
        if isinstance(method, failing):
            raise RuntimeError(f"{type(method).__name__} down")
        if isinstance(method, SendMessage):
            return Message.model_validate(
                {"message_id": 1, "date": 1, "chat": {"id": _CHAT, "type": "private"}}
            )
        return True

    return responder


@pytest.mark.asyncio
async def test_failed_callback_answer_is_a_warning_and_the_chat_reply_still_goes(
    log_records: list[Record],
) -> None:
    bot = build_fake_bot(responder=_responder_failing_on(AnswerCallbackQuery))

    await _dispatcher().feed_update(
        bot, make_callback_update("boom", update_id=14, chat_id=_CHAT, user_id=_USER)
    )

    assert [m.text for m in _sent(bot)] == [texts.UNEXPECTED_ERROR_TEXT]
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert "Failed to answer the callback" in warnings[0]["message"]
    assert "AnswerCallbackQuery down" in warnings[0]["message"]
    assert len(_errors(log_records)) == 1


@pytest.mark.asyncio
async def test_failed_chat_reply_still_answers_the_callback_with_one_warning(
    log_records: list[Record],
) -> None:
    bot = build_fake_bot(responder=_responder_failing_on(SendMessage))

    await _dispatcher().feed_update(
        bot, make_callback_update("boom", update_id=15, chat_id=_CHAT, user_id=_USER)
    )

    assert len(_answers(bot)) == 1
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert "Failed to deliver the error reply" in warnings[0]["message"]


@pytest.mark.asyncio
async def test_both_replies_failing_gives_two_warnings_and_no_propagation(
    log_records: list[Record],
) -> None:
    bot = build_fake_bot(
        responder=_responder_failing_on(SendMessage, AnswerCallbackQuery)
    )

    await _dispatcher().feed_update(
        bot, make_callback_update("boom", update_id=16, chat_id=_CHAT, user_id=_USER)
    )

    messages = [w["message"] for w in _warnings(log_records)]
    assert len(messages) == 2
    assert "Failed to deliver the error reply" in messages[0]
    assert "Failed to answer the callback" in messages[1]
    assert len(_errors(log_records)) == 1


@pytest.mark.asyncio
async def test_chat_reply_is_attempted_before_the_callback_answer() -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()

    await dispatcher.feed_update(
        bot, make_callback_update("boom", update_id=17, chat_id=_CHAT, user_id=_USER)
    )

    kinds = [type(r) for r in bot.session.requests]  # type: ignore[attr-defined]
    assert kinds == [SendMessage, AnswerCallbackQuery]


@pytest.mark.asyncio
async def test_fsm_state_is_left_untouched_by_the_error_handler() -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()
    key = StorageKey(bot_id=bot.id, chat_id=_CHAT, user_id=_USER)
    await dispatcher.storage.set_state(key, "AddProduct:confirming")
    await dispatcher.storage.set_data(key, {"draft": 1})

    await dispatcher.feed_update(
        bot,
        make_callback_update("boom", update_id=9, chat_id=_CHAT, user_id=_USER),
    )

    assert await dispatcher.storage.get_state(key) == "AddProduct:confirming"
    assert await dispatcher.storage.get_data(key) == {"draft": 1}


@pytest.mark.asyncio
async def test_cancelled_error_is_not_swallowed(log_records: list[Record]) -> None:
    dispatcher, bot = _dispatcher(), build_fake_bot()

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.feed_update(
            bot,
            make_message_update(
                text="cancel-me", update_id=6, chat_id=_CHAT, user_id=_USER
            ),
        )

    assert _errors(log_records) == []
    assert _sent(bot) == []
