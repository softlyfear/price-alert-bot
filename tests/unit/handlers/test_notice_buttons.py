"""Buttons under a price notification (PAB-097 AC1, AC4, AC5).

Everything goes through ``Dispatcher.feed_update``. The replaced boundary is
``TrackingService`` (``card_service`` fixture). The notification is a record of
a trigger, so both buttons answer with a NEW message and never edit it; the FSM
of the pressing user is not touched. The dialog uses a user id that differs
from the chat id, so a handler reading the wrong id is visible.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import SendMessage

from app.bot import texts
from app.bot.keyboards import CB_NOTICE_MENU
from app.bot.states import AddProduct
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import ADD_DATA
from tests.unit.handlers.card_dialog_support import ADD_STATES
from tests.unit.handlers.card_dialog_support import CARD_NAV
from tests.unit.handlers.card_dialog_support import CARD_TEXT
from tests.unit.handlers.card_dialog_support import EDIT_DATA
from tests.unit.handlers.card_dialog_support import EDIT_STATE
from tests.unit.handlers.card_dialog_support import MENU
from tests.unit.handlers.card_dialog_support import PRODUCT_ID
from tests.unit.handlers.card_dialog_support import callbacks
from tests.unit.handlers.card_dialog_support import rows

_OPEN = f"ncard:{PRODUCT_ID}"
_FSM_CASES: list[Any] = [
    pytest.param(None, {}, id="empty"),
    *(pytest.param(state, dict(ADD_DATA), id=state) for state in ADD_STATES),
    pytest.param(EDIT_STATE, dict(EDIT_DATA), id="edit"),
]


# --- "Открыть товар" ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_product_sends_one_new_card_message_and_edits_nothing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(_OPEN)

    assert [type(r) for r in card_dialog.requests] == [
        AnswerCallbackQuery,
        SendMessage,
    ]
    shown = card_dialog.sent[0]
    assert shown.chat_id == _CHAT
    assert shown.text == CARD_TEXT
    assert shown.parse_mode is None
    assert rows(shown)[-1] == CARD_NAV
    assert card_dialog.edits == []
    assert len(card_dialog.callback_answers) == 1
    assert card_dialog.callback_answers[0].text is None


@pytest.mark.asyncio
async def test_open_product_reads_the_card_for_the_pressing_user_not_the_chat(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(_OPEN)

    card_service.get_product_card.assert_awaited_once_with(_USER, PRODUCT_ID)
    assert _USER != _CHAT


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "data"), _FSM_CASES)
async def test_open_product_leaves_the_fsm_state_and_data_untouched(
    card_dialog: _Dialog,
    card_service: MagicMock,
    state: str | None,
    data: dict[str, Any],
) -> None:
    await card_dialog.set_state(state, dict(data))

    await card_dialog.press(_OPEN)

    assert await card_dialog.state() == state
    assert await card_dialog.data() == data
    assert len(card_dialog.sent) == 1


@pytest.mark.asyncio
async def test_open_product_of_someone_else_or_deleted_gives_one_popup_only(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = None

    await card_dialog.press(_OPEN)

    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.LIST_GONE_ALERT_TEXT, True)
    ]
    card_service.get_product_card.assert_awaited_once_with(_USER, PRODUCT_ID)


@pytest.mark.asyncio
async def test_open_product_without_a_message_pops_up_and_sends_nothing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(_OPEN, with_message=False)

    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", ["ncard", "ncard:", "ncard:0", "ncard:-1", "ncard:abc", "ncard:2147483648"]
)
async def test_broken_open_product_payload_is_stale_and_never_reaches_the_service(
    card_dialog: _Dialog, card_service: MagicMock, data: str
) -> None:
    await card_dialog.press(data)

    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    card_service.get_product_card.assert_not_called()


# --- "Главное меню" -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_menu_sends_one_new_start_message_and_edits_nothing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(CB_NOTICE_MENU)

    assert [type(r) for r in card_dialog.requests] == [
        AnswerCallbackQuery,
        SendMessage,
    ]
    shown = card_dialog.sent[0]
    assert shown.chat_id == _CHAT
    assert shown.text == texts.START_TEXT
    assert shown.parse_mode is None
    assert callbacks(shown) == MENU
    assert card_dialog.edits == []
    assert len(card_dialog.callback_answers) == 1
    assert card_dialog.callback_answers[0].show_alert is not True
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "data"), _FSM_CASES)
async def test_notice_menu_leaves_the_fsm_state_and_data_untouched(
    card_dialog: _Dialog, state: str | None, data: dict[str, Any]
) -> None:
    await card_dialog.set_state(state, dict(data))

    await card_dialog.press(CB_NOTICE_MENU)

    assert await card_dialog.state() == state
    assert await card_dialog.data() == data
    assert [m.text for m in card_dialog.sent] == [texts.START_TEXT]


@pytest.mark.asyncio
async def test_notice_menu_without_a_message_pops_up_and_sends_nothing(
    card_dialog: _Dialog,
) -> None:
    await card_dialog.press(CB_NOTICE_MENU, with_message=False)

    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.MENU_STALE_ALERT_TEXT, True)
    ]


def test_add_product_state_names_used_by_the_fsm_cases_exist() -> None:
    """Guards the string literals of ``ADD_STATES`` against a renamed state."""
    assert set(ADD_STATES) == {
        AddProduct.waiting_link.state,
        AddProduct.waiting_target_price.state,
        AddProduct.confirming.state,
    }
