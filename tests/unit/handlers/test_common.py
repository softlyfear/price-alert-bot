"""Tests for app.handlers.common: /start, /help, /cancel (AC2, PAB-067).

Every command is exercised through ``Dispatcher.feed_update`` on a fake
``Bot`` session (``tests/unit/conftest.py``), never by calling the handler
coroutine directly -- so a regression in filter wiring (``@router.message``,
``Command(...)``) would be caught, not just a regression in the handler
body.

``real_dispatcher`` (``tests/unit/conftest.py``) is the real
``create_dispatcher()`` output, built fresh for each test: routers are
assembled by factories, not module-level singletons, so each test gets its
own ``Dispatcher`` and its own ``MemoryStorage``. Tests below still use
their own ``chat_id``/``user_id`` pair (fixture ``unique_chat_id``) for
clarity, though a collision is no longer possible even without it.
"""

from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.domain.money import PRICE_DISCLAIMER_FULL
from app.services.tracking import TrackingService
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_callback_update
from tests.unit.conftest import make_command_update
from tests.unit.conftest import make_message_update


@pytest.fixture
def bot() -> Bot:
    return build_fake_bot()


@pytest.fixture
def tracking(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """``TrackingService`` double (the boundary); the effective limit is 50."""
    mock = MagicMock(spec=TrackingService)
    mock.product_limit.return_value = 50
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: mock)
    return mock


def _storage_key(bot_id: int, chat_and_user_id: int) -> StorageKey:
    return StorageKey(bot_id=bot_id, chat_id=chat_and_user_id, user_id=chat_and_user_id)


def _sent_texts(bot_requests: list[object]) -> list[str]:
    return [req.text for req in bot_requests if isinstance(req, SendMessage)]


@pytest.mark.asyncio
async def test_start_replies_with_text_containing_full_disclaimer(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
) -> None:
    await real_dispatcher.feed_update(
        bot,
        make_command_update("start", chat_id=unique_chat_id, user_id=unique_chat_id),
    )

    sent = _sent_texts(bot.session.requests)  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert sent[0] == texts.START_TEXT
    assert PRICE_DISCLAIMER_FULL in sent[0]
    assert texts.SOUND_REMINDER_TEXT in sent[0]
    assert "/add" not in sent[0]
    assert "/list" not in sent[0]


@pytest.mark.asyncio
async def test_help_replies_with_text_containing_full_disclaimer(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int, tracking: MagicMock
) -> None:
    """Mutation target: replacing the reply with a copy of the help text
    that has one character changed instead of the imported builder --
    caught here because the reply is compared for exact equality with
    ``texts.help_text(50)``, and ``PRICE_DISCLAIMER_FULL in sent[0]`` catches
    a change specifically inside the disclaimer substring.
    """
    await real_dispatcher.feed_update(
        bot, make_command_update("help", chat_id=unique_chat_id, user_id=unique_chat_id)
    )

    sent = _sent_texts(bot.session.requests)  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert sent[0] == texts.help_text(50)
    assert PRICE_DISCLAIMER_FULL in sent[0]
    assert texts.SOUND_REMINDER_TEXT in sent[0]


@pytest.mark.asyncio
async def test_start_does_not_change_fsm_state(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
) -> None:
    key = _storage_key(bot.id, unique_chat_id)
    await real_dispatcher.storage.set_state(key, "some-dialog-state")

    await real_dispatcher.feed_update(
        bot,
        make_command_update("start", chat_id=unique_chat_id, user_id=unique_chat_id),
    )

    assert await real_dispatcher.storage.get_state(key) == "some-dialog-state"


@pytest.mark.asyncio
async def test_help_does_not_change_fsm_state(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int, tracking: MagicMock
) -> None:
    key = _storage_key(bot.id, unique_chat_id)
    await real_dispatcher.storage.set_state(key, "some-dialog-state")

    await real_dispatcher.feed_update(
        bot, make_command_update("help", chat_id=unique_chat_id, user_id=unique_chat_id)
    )

    assert await real_dispatcher.storage.get_state(key) == "some-dialog-state"


@pytest.mark.asyncio
async def test_cancel_with_active_state_clears_it_and_replies_with_cancel_text(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
) -> None:
    key = _storage_key(bot.id, unique_chat_id)
    await real_dispatcher.storage.set_state(key, "some-dialog-state")

    await real_dispatcher.feed_update(
        bot,
        make_command_update("cancel", chat_id=unique_chat_id, user_id=unique_chat_id),
    )

    assert await real_dispatcher.storage.get_state(key) is None
    sent = _sent_texts(bot.session.requests)  # type: ignore[attr-defined]
    assert sent == [texts.CANCEL_TEXT]


@pytest.mark.asyncio
async def test_cancel_without_active_state_replies_with_nothing_to_cancel_text(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
) -> None:
    key = _storage_key(bot.id, unique_chat_id)
    assert await real_dispatcher.storage.get_state(key) is None

    await real_dispatcher.feed_update(
        bot,
        make_command_update("cancel", chat_id=unique_chat_id, user_id=unique_chat_id),
    )

    sent = _sent_texts(bot.session.requests)  # type: ignore[attr-defined]
    assert sent == [texts.NOTHING_TO_CANCEL_TEXT]
    assert texts.NOTHING_TO_CANCEL_TEXT != texts.CANCEL_TEXT


# --- main menu (PAB-092) -------------------------------------------------------

_MENU = ["menu:add", "menu:list", "menu:help"]


def _requests(bot: Bot) -> list[Any]:
    return cast(Any, bot.session).requests  # type: ignore[no-any-return]


def _callbacks(request: SendMessage | EditMessageText) -> list[str | None]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["start", "help", "cancel"])
async def test_command_replies_carry_the_main_menu(
    real_dispatcher: Dispatcher,
    bot: Bot,
    unique_chat_id: int,
    command: str,
    tracking: MagicMock,
) -> None:
    await real_dispatcher.feed_update(
        bot,
        make_command_update(command, chat_id=unique_chat_id, user_id=unique_chat_id),
    )

    sent = [r for r in _requests(bot) if isinstance(r, SendMessage)]
    assert len(sent) == 1
    assert _callbacks(sent[0]) == _MENU


@pytest.mark.asyncio
async def test_cancel_with_active_state_replies_with_the_main_menu(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
) -> None:
    await real_dispatcher.storage.set_state(
        _storage_key(bot.id, unique_chat_id), "some-dialog-state"
    )

    await real_dispatcher.feed_update(
        bot,
        make_command_update("cancel", chat_id=unique_chat_id, user_id=unique_chat_id),
    )

    sent = [r for r in _requests(bot) if isinstance(r, SendMessage)]
    assert [m.text for m in sent] == [texts.CANCEL_TEXT]
    assert _callbacks(sent[0]) == _MENU


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [3, 77])
async def test_help_command_names_the_effective_limit_of_the_sender(
    real_dispatcher: Dispatcher,
    bot: Bot,
    tracking: MagicMock,
    unique_chat_id: int,
    limit: int,
) -> None:
    tracking.product_limit.return_value = limit

    await real_dispatcher.feed_update(
        bot, make_command_update("help", chat_id=unique_chat_id, user_id=unique_chat_id)
    )

    sent = _sent_texts(_requests(bot))
    assert sent == [texts.help_text(limit)]
    assert f"Лимит товаров: {limit}." in sent[0]
    tracking.product_limit.assert_awaited_once_with(unique_chat_id)


@pytest.mark.asyncio
async def test_help_command_asks_for_the_sender_not_the_chat(
    real_dispatcher: Dispatcher, bot: Bot, tracking: MagicMock
) -> None:
    await real_dispatcher.feed_update(
        bot, make_command_update("help", chat_id=111, user_id=222)
    )

    tracking.product_limit.assert_awaited_once_with(222)


@pytest.mark.asyncio
async def test_help_command_without_a_sender_is_ignored_and_service_untouched(
    real_dispatcher: Dispatcher, bot: Bot, tracking: MagicMock
) -> None:
    await real_dispatcher.feed_update(
        bot, make_message_update(text="/help", user_id=None)
    )

    assert _requests(bot) == []
    assert tracking.mock_calls == []


@pytest.mark.asyncio
async def test_menu_start_button_edits_to_the_start_screen_and_keeps_fsm(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
) -> None:
    key = _storage_key(bot.id, unique_chat_id)
    await real_dispatcher.storage.set_state(key, "some-dialog-state")
    await real_dispatcher.storage.set_data(key, {"draft": 1})

    await real_dispatcher.feed_update(
        bot,
        make_callback_update(
            "menu:start", chat_id=unique_chat_id, user_id=unique_chat_id
        ),
    )

    edits = [r for r in _requests(bot) if isinstance(r, EditMessageText)]
    assert [(e.text, e.parse_mode) for e in edits] == [(texts.START_TEXT, None)]
    assert _callbacks(edits[0]) == _MENU
    assert len([r for r in _requests(bot) if isinstance(r, AnswerCallbackQuery)]) == 1
    assert await real_dispatcher.storage.get_state(key) == "some-dialog-state"
    assert await real_dispatcher.storage.get_data(key) == {"draft": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [3, 77])
async def test_menu_help_button_edits_to_help_with_the_effective_limit_and_keeps_fsm(
    real_dispatcher: Dispatcher,
    bot: Bot,
    tracking: MagicMock,
    unique_chat_id: int,
    limit: int,
) -> None:
    tracking.product_limit.return_value = limit
    dispatcher = real_dispatcher
    key = _storage_key(bot.id, unique_chat_id)
    await dispatcher.storage.set_state(key, "some-dialog-state")
    await dispatcher.storage.set_data(key, {"draft": 1})

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            "menu:help", chat_id=unique_chat_id, user_id=unique_chat_id
        ),
    )

    edits = [r for r in _requests(bot) if isinstance(r, EditMessageText)]
    assert [(e.text, e.parse_mode) for e in edits] == [(texts.help_text(limit), None)]
    assert f"Лимит товаров: {limit}." in edits[0].text
    assert _callbacks(edits[0]) == _MENU
    assert len([r for r in _requests(bot) if isinstance(r, AnswerCallbackQuery)]) == 1
    assert await dispatcher.storage.get_state(key) == "some-dialog-state"
    assert await dispatcher.storage.get_data(key) == {"draft": 1}
    tracking.product_limit.assert_awaited_once_with(unique_chat_id)


@pytest.mark.asyncio
async def test_menu_help_button_asks_for_the_presser_not_the_chat(
    real_dispatcher: Dispatcher, bot: Bot, tracking: MagicMock
) -> None:
    await real_dispatcher.feed_update(
        bot, make_callback_update("menu:help", chat_id=111, user_id=222)
    )

    tracking.product_limit.assert_awaited_once_with(222)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["menu:", "menu:nope", "menu:add:1", "menu:ADD"])
async def test_unknown_menu_button_gives_a_popup_and_changes_nothing(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int, data: str
) -> None:
    key = _storage_key(bot.id, unique_chat_id)
    await real_dispatcher.storage.set_state(key, "some-dialog-state")

    await real_dispatcher.feed_update(
        bot,
        make_callback_update(data, chat_id=unique_chat_id, user_id=unique_chat_id),
    )

    requests = _requests(bot)
    assert [type(r) for r in requests] == [AnswerCallbackQuery]
    assert (requests[0].text, requests[0].show_alert) == (
        texts.MENU_STALE_ALERT_TEXT,
        True,
    )
    assert await real_dispatcher.storage.get_state(key) == "some-dialog-state"
