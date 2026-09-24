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

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import SendMessage

from app.bot import texts
from app.domain.money import PRICE_DISCLAIMER_FULL
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_command_update


@pytest.fixture
def bot() -> Bot:
    return build_fake_bot()


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


@pytest.mark.asyncio
async def test_help_replies_with_text_containing_full_disclaimer(
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
) -> None:
    """Mutation target: replacing the reply with a copy of ``HELP_TEXT``
    that has one character changed instead of the imported constant --
    caught here because the reply is compared for exact equality with
    ``texts.HELP_TEXT``, and ``PRICE_DISCLAIMER_FULL in sent[0]`` catches a
    change specifically inside the disclaimer substring.
    """
    await real_dispatcher.feed_update(
        bot, make_command_update("help", chat_id=unique_chat_id, user_id=unique_chat_id)
    )

    sent = _sent_texts(bot.session.requests)  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert sent[0] == texts.HELP_TEXT
    assert PRICE_DISCLAIMER_FULL in sent[0]


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
    real_dispatcher: Dispatcher, bot: Bot, unique_chat_id: int
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
