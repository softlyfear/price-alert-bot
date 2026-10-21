"""The two "Отмена" buttons must not end each other's dialog (PAB-096, tail (c)).

Matrix of PAB-096 AC6: the /add "Отмена" (``add:cancel``) and the threshold
edit "Отмена" (``ecancel:<product_id>``) against every dialog state. A stale
press gives exactly one popup and changes nothing: no edit, no new message,
and the state and data stay byte-for-byte as they were. Also the commands in
the edit state (AC7) and the non-text input without a product id.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery

from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from tests.unit.conftest import make_message_update
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import ADD_DATA
from tests.unit.handlers.card_dialog_support import ADD_STATES
from tests.unit.handlers.card_dialog_support import CARD_TEXT
from tests.unit.handlers.card_dialog_support import EDIT_DATA
from tests.unit.handlers.card_dialog_support import EDIT_STATE
from tests.unit.handlers.card_dialog_support import MENU
from tests.unit.handlers.card_dialog_support import PRODUCT_ID
from tests.unit.handlers.card_dialog_support import callbacks

_EDIT_CANCEL = f"ecancel:{PRODUCT_ID}"
_STALE = [(texts.LIST_STALE_BUTTON_ALERT_TEXT, True)]


async def _assert_stale_popup_only(
    dialog: _Dialog, state: str | None, data: dict[str, Any]
) -> None:
    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == _STALE
    assert [type(r) for r in dialog.requests] == [AnswerCallbackQuery]
    assert await dialog.state() == state
    assert await dialog.data() == data


# --- add:cancel ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_cancel_under_a_running_edit_is_stale_and_changes_nothing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.set_state(EDIT_STATE, dict(EDIT_DATA))

    await card_dialog.press(CB_ADD_CANCEL)

    await _assert_stale_popup_only(card_dialog, EDIT_STATE, EDIT_DATA)
    card_service.change_alert_target.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [*ADD_STATES, None])
async def test_add_cancel_in_the_add_dialog_or_without_state_still_cancels(
    card_dialog: _Dialog, card_service: MagicMock, state: str | None
) -> None:
    await card_dialog.set_state(state, dict(ADD_DATA) if state else {})

    await card_dialog.press(CB_ADD_CANCEL)

    assert [m.text for m in card_dialog.sent] == [texts.CANCEL_TEXT]
    assert callbacks(card_dialog.sent[0]) == MENU
    assert len(card_dialog.callback_answers) == 1
    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}


# --- the edit "Отмена" ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ADD_STATES)
async def test_edit_cancel_under_a_running_add_dialog_is_stale_and_keeps_its_draft(
    card_dialog: _Dialog, card_service: MagicMock, state: str
) -> None:
    await card_dialog.set_state(state, dict(ADD_DATA))

    await card_dialog.press(_EDIT_CANCEL)

    await _assert_stale_popup_only(card_dialog, state, ADD_DATA)
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
async def test_edit_cancel_of_another_product_under_a_running_edit_is_stale(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.set_state(
        EDIT_STATE, {"alert_id": 21, "product_id": PRODUCT_ID + 1}
    )

    await card_dialog.press(_EDIT_CANCEL)

    await _assert_stale_popup_only(
        card_dialog, EDIT_STATE, {"alert_id": 21, "product_id": PRODUCT_ID + 1}
    )
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
async def test_edit_cancel_under_an_edit_without_a_product_id_is_stale(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.set_state(EDIT_STATE, {"alert_id": 11})

    await card_dialog.press(_EDIT_CANCEL)

    await _assert_stale_popup_only(card_dialog, EDIT_STATE, {"alert_id": 11})


@pytest.mark.asyncio
async def test_edit_cancel_of_the_same_product_puts_the_card_back_and_ends_the_edit(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.set_state(EDIT_STATE, dict(EDIT_DATA))

    await card_dialog.press(_EDIT_CANCEL)

    card_service.get_product_card.assert_awaited_once_with(_USER, PRODUCT_ID)
    assert card_dialog.sent == []
    assert [(e.message_id, e.text) for e in card_dialog.edits] == [
        (_CARD_ID, CARD_TEXT)
    ]
    assert len(card_dialog.callback_answers) == 1
    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}


@pytest.mark.asyncio
async def test_edit_cancel_without_state_says_cancelled_with_the_main_menu(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(_EDIT_CANCEL)

    card_service.get_product_card.assert_not_called()
    assert [e.text for e in card_dialog.edits] == [texts.CANCEL_TEXT]
    assert callbacks(card_dialog.edits[0]) == MENU
    assert len(card_dialog.callback_answers) == 1
    assert await card_dialog.state() is None


_BROKEN = [
    "ecancel",
    "ecancel:",
    "ecancel:x",
    "ecancel:0",
    "ecancel:5:6",
    "edit:cancel",
]
_ALL_STATES: list[tuple[str | None, dict[str, Any]]] = [
    (None, {}),
    (EDIT_STATE, EDIT_DATA),
    *[(state, ADD_DATA) for state in ADD_STATES],
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", _BROKEN)
@pytest.mark.parametrize(("state", "data"), _ALL_STATES)
async def test_broken_or_legacy_edit_cancel_is_stale_in_every_state(
    card_dialog: _Dialog,
    card_service: MagicMock,
    payload: str,
    state: str | None,
    data: dict[str, Any],
) -> None:
    await card_dialog.set_state(state, dict(data))

    await card_dialog.press(payload)

    await _assert_stale_popup_only(card_dialog, state, data)
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
async def test_edit_cancel_without_a_message_under_a_running_add_dialog_is_stale(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    # Without a message Telegram gives no chat, so aiogram keys the state by user.
    key = StorageKey(bot_id=card_dialog.bot.id, chat_id=_USER, user_id=_USER)
    storage = card_dialog.dispatcher.storage
    await storage.set_state(key, ADD_STATES[1])
    await storage.set_data(key, dict(ADD_DATA))

    await card_dialog.press(_EDIT_CANCEL, with_message=False)

    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == _STALE
    assert await storage.get_state(key) == ADD_STATES[1]
    assert await storage.get_data(key) == ADD_DATA


# --- "Отмена" under an answer to a wrong amount works --------------------------------


@pytest.mark.asyncio
async def test_cancel_under_the_wrong_amount_reply_of_the_add_dialog_cancels_it(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(f"aadd:{PRODUCT_ID}")
    await card_dialog.say("не число")
    (cancel_data,) = callbacks(card_dialog.sent[-1])
    assert cancel_data is not None

    await card_dialog.press(cancel_data, message_id=1000)

    assert card_dialog.sent[-1].text == texts.CANCEL_TEXT
    assert await card_dialog.state() is None


@pytest.mark.asyncio
async def test_cancel_under_the_wrong_amount_reply_of_the_edit_dialog_restores_the_card(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.set_state(EDIT_STATE, dict(EDIT_DATA))
    await card_dialog.say("не число")
    (cancel_data,) = callbacks(card_dialog.sent[-1])
    assert cancel_data == _EDIT_CANCEL
    card_service.change_alert_target.assert_not_called()

    await card_dialog.press(cancel_data, message_id=1000)

    assert [e.text for e in card_dialog.edits] == [CARD_TEXT]
    assert await card_dialog.state() is None


# --- on_edit_not_text without a product id --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", [{}, {"alert_id": 11}, {"alert_id": 11, "product_id": "5"}]
)
async def test_non_text_in_an_edit_without_a_product_id_reports_stale_and_clears(
    card_dialog: _Dialog, card_service: MagicMock, data: dict[str, Any]
) -> None:
    await card_dialog.set_state(EDIT_STATE, dict(data))

    await card_dialog.feed(
        make_message_update(photo=True, update_id=50, chat_id=_CHAT, user_id=_USER)
    )

    assert [m.text for m in card_dialog.sent] == [texts.EDIT_STALE_TEXT]
    assert callbacks(card_dialog.sent[0]) == MENU
    assert await card_dialog.state() is None
    card_service.change_alert_target.assert_not_called()


# --- AC7: commands in the edit state ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("start", texts.START_TEXT),
        ("help", texts.help_text(50)),
        ("list", texts.LIST_EMPTY_TEXT),
    ],
)
async def test_commands_in_the_edit_state_answer_themselves_and_keep_the_edit(
    card_dialog: _Dialog,
    card_service: MagicMock,
    command: str,
    expected: str,
) -> None:
    await card_dialog.set_state(EDIT_STATE, dict(EDIT_DATA))

    await card_dialog.command(command)

    assert len(card_dialog.sent) == 1
    assert card_dialog.sent[0].text == expected
    card_service.change_alert_target.assert_not_called()
    assert await card_dialog.state() == EDIT_STATE
    assert await card_dialog.data() == EDIT_DATA
