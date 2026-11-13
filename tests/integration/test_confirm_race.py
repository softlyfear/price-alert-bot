"""Two taps on the same confirmation card must not reopen the dialog."""

from __future__ import annotations

import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import Update
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import texts
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.setup import create_dispatcher
from app.bot.states import AddProduct
from app.core.database import create_sqlite_engine
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.product import Product
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_callback_update
from tests.unit.conftest import send_message_responder
from tests.unit.wb_access_stub import stub_client_factory

_CHAT = 1000
_USER = 1000
_CARD_ID = 5


def _confirm_update(update_id: int, callback_id: str) -> Update:
    update = make_callback_update(
        CB_ADD_CONFIRM,
        update_id=update_id,
        chat_id=_CHAT,
        user_id=_USER,
        message_id=_CARD_ID,
    )
    raw = update.model_dump()
    raw["callback_query"]["id"] = callback_id
    return type(update).model_validate(raw)


@pytest.mark.asyncio
async def test_second_confirm_tap_does_not_reopen_the_finished_dialog(
    database_path: Path,
) -> None:
    """The losing tap keeps the saved alert and leaves the dialog closed.

    Both presses pass the FSM check. SQLite serializes the writes, so the
    second learns about the duplicate only after the first has cleared the
    dialog and committed. Reopening the price step would show a duplicate
    warning over the success message and then treat the next text as a price.
    """
    engine = create_sqlite_engine(database_path, busy_timeout_seconds=5)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    storage = MemoryStorage()
    bot = build_fake_bot(responder=send_message_responder())
    async with httpx.AsyncClient() as http_client:
        dispatcher = create_dispatcher(
            storage, factory, http_client, 50, stub_client_factory()
        )
        dispatcher["display_timezone"] = ZoneInfo("Europe/Moscow")
        key = StorageKey(bot_id=bot.id, chat_id=_CHAT, user_id=_USER)
        await storage.set_state(key, AddProduct.confirming.state)
        await storage.set_data(
            key,
            {
                "marketplace": "wb",
                "article": 12_345_678,
                "product_name": "Шапка",
                "current_price": 199_000,
                "target_price": 150_000,
                "confirm_message_id": _CARD_ID,
                "wb_size_id": 0,
                "wb_size_name": None,
                "price_fetched_at": None,
            },
        )
        try:
            await asyncio.gather(
                dispatcher.feed_update(bot, _confirm_update(1, "cb-1")),
                dispatcher.feed_update(bot, _confirm_update(2, "cb-2")),
            )
            assert await storage.get_state(key) is None
            assert await storage.get_data(key) == {}
            async with factory() as session:
                products = await session.scalar(
                    select(func.count()).select_from(Product)
                )
                alerts = await session.scalar(select(func.count()).select_from(Alert))
            assert products == 1
            assert alerts == 1
            sent = [
                method.text
                for method in bot.session.requests  # type: ignore[attr-defined]
                if isinstance(method, SendMessage)
            ]
            edited = [
                method.text
                for method in bot.session.requests  # type: ignore[attr-defined]
                if isinstance(method, EditMessageText)
            ]
            answers = [
                method
                for method in bot.session.requests  # type: ignore[attr-defined]
                if isinstance(method, AnswerCallbackQuery)
            ]
            assert sent == [texts.added_text("Шапка", 150_000, AlertDirection.below)]
            assert texts.ALERT_DUPLICATE_TEXT not in edited
            assert texts.ALERT_DUPLICATE_TEXT not in sent
            assert len(answers) == 2
            assert texts.UNEXPECTED_ERROR_TEXT not in sent
        finally:
            await engine.dispose()
            await bot.session.close()
