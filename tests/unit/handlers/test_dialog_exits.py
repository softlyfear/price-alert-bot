"""Exit buttons of a running dialog: ``dlg:menu`` and ``dlg:list`` (PAB-098 AC4).

The duplicate-threshold screen offers "← К списку товаров" and "Главное меню".
Both end whatever dialog is running and show the target screen in place of the
pressed message. The replaced boundary is ``TrackingService`` (``card_service``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText

from app.bot import texts
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import ADD_DATA
from tests.unit.handlers.card_dialog_support import EDIT_DATA
from tests.unit.handlers.card_dialog_support import EDIT_STATE
from tests.unit.handlers.card_dialog_support import MENU
from tests.unit.handlers.card_dialog_support import NB
from tests.unit.handlers.card_dialog_support import callbacks
from tests.unit.handlers.card_dialog_support import page_of
from tests.unit.handlers.card_dialog_support import product
from tests.unit.handlers.card_dialog_support import rows

_DIALOG_STATES: list[tuple[str | None, dict[str, Any]]] = [
    ("AddProduct:waiting_link", {}),
    ("AddProduct:waiting_target_price", ADD_DATA),
    ("AddProduct:confirming", ADD_DATA),
    (EDIT_STATE, EDIT_DATA),
    (None, {}),
]
_IDS = ["waiting-link", "waiting-target-price", "confirming", "edit-alert", "empty"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "data"), _DIALOG_STATES, ids=_IDS)
async def test_dialog_menu_clears_the_dialog_and_shows_the_main_menu_in_place(
    card_dialog: _Dialog, state: str | None, data: dict[str, Any]
) -> None:
    await card_dialog.set_state(state, dict(data))

    await card_dialog.press("dlg:menu")

    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}
    assert card_dialog.sent == []
    edit = card_dialog.edits[0]
    assert len(card_dialog.edits) == 1
    assert (edit.chat_id, edit.message_id) == (_CHAT, _CARD_ID)
    assert edit.text == texts.START_TEXT
    assert edit.parse_mode is None
    assert callbacks(edit) == MENU
    assert len(card_dialog.callback_answers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "data"), _DIALOG_STATES, ids=_IDS)
async def test_dialog_list_clears_the_dialog_and_shows_the_list_of_the_presser(
    card_dialog: _Dialog,
    card_service: MagicMock,
    state: str | None,
    data: dict[str, Any],
) -> None:
    card_service.list_page.return_value = page_of([product()])
    await card_dialog.set_state(state, dict(data))

    await card_dialog.press("dlg:list")

    card_service.list_page.assert_awaited_once_with(_USER, 1)
    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}
    assert card_dialog.sent == []
    assert len(card_dialog.edits) == 1
    edit = card_dialog.edits[0]
    assert (edit.chat_id, edit.message_id) == (_CHAT, _CARD_ID)
    assert edit.text == (
        "Вы следите за товарами: 1.\nЦены на 14:05\nЦена без учёта персональных скидок."
    )
    assert [b for row in rows(edit) for b in row][0] == (
        f"1{NB}755{NB}₽ · Из базы <b>1",
        "pcard:5",
    )
    assert len(card_dialog.callback_answers) == 1


@pytest.mark.asyncio
async def test_dialog_list_of_a_user_without_products_shows_the_empty_text(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of([])

    await card_dialog.press("dlg:list")

    assert [e.text for e in card_dialog.edits] == [texts.LIST_EMPTY_TEXT]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["dlg:", "dlg:nope", "dlg:list:1", "dlg:MENU"])
@pytest.mark.parametrize(("state", "payload"), _DIALOG_STATES[1:4], ids=_IDS[1:4])
async def test_unknown_dialog_exit_gives_a_popup_and_leaves_the_dialog_alone(
    card_dialog: _Dialog,
    card_service: MagicMock,
    data: str,
    state: str | None,
    payload: dict[str, Any],
) -> None:
    await card_dialog.set_state(state, dict(payload))

    await card_dialog.press(data)

    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    answer = card_dialog.callback_answers[0]
    assert (answer.text, answer.show_alert) == (texts.MENU_STALE_ALERT_TEXT, True)
    assert await card_dialog.state() == state
    assert await card_dialog.data() == payload
    card_service.list_page.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["dlg:menu", "dlg:list"])
async def test_dialog_exit_without_a_message_gives_a_popup_and_still_ends_the_dialog(
    card_dialog: _Dialog, data: str
) -> None:
    # No message: aiogram falls back to the chat_id == user_id FSM key.
    key = StorageKey(bot_id=card_dialog.bot.id, chat_id=_USER, user_id=_USER)
    storage = card_dialog.dispatcher.storage
    await storage.set_state(key, EDIT_STATE)
    await storage.set_data(key, dict(EDIT_DATA))

    await card_dialog.press(data, with_message=False)

    assert not [r for r in card_dialog.requests if isinstance(r, EditMessageText)]
    assert card_dialog.sent == []
    answer = card_dialog.callback_answers[0]
    assert (answer.text, answer.show_alert) == (texts.MENU_STALE_ALERT_TEXT, True)
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["menu:start", "menu:help"])
async def test_menu_buttons_still_leave_the_running_dialog_untouched(
    card_dialog: _Dialog, data: str
) -> None:
    await card_dialog.set_state(EDIT_STATE, dict(EDIT_DATA))

    await card_dialog.press(data)

    assert await card_dialog.state() == EDIT_STATE
    assert await card_dialog.data() == EDIT_DATA
