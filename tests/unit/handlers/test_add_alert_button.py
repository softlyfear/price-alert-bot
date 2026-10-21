"""The "Добавить порог" button of the product card (PAB-096).

Everything goes through ``Dispatcher.feed_update``. The replaced boundary is
``TrackingService`` (``card_service``): the card it returns carries the name
and price "from the database" (``DB_NAME``, ``DB_PRICE``), which differ from
anything a marketplace preview could give, so a value taken from the wrong
place shows up in the screen and in the FSM data.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage

from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.domain.exceptions import ProductLimitExceededError
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from tests.unit.conftest import sent_message_id
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import ADD_DATA
from tests.unit.handlers.card_dialog_support import ADD_STATES
from tests.unit.handlers.card_dialog_support import CARD_NAV
from tests.unit.handlers.card_dialog_support import DB_ARTICLE
from tests.unit.handlers.card_dialog_support import DB_FETCHED_AT
from tests.unit.handlers.card_dialog_support import DB_NAME
from tests.unit.handlers.card_dialog_support import DB_PRICE
from tests.unit.handlers.card_dialog_support import EDIT_DATA
from tests.unit.handlers.card_dialog_support import EDIT_STATE
from tests.unit.handlers.card_dialog_support import NB
from tests.unit.handlers.card_dialog_support import PRODUCT_ID
from tests.unit.handlers.card_dialog_support import callbacks
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import rows

_DRAFT_FROM_DB: dict[str, Any] = {
    "marketplace": "wb",
    "article": DB_ARTICLE,
    "product_name": DB_NAME,
    "current_price": DB_PRICE,
    "price_fetched_at": DB_FETCHED_AT.isoformat(),
}
_START_STATES: list[tuple[str | None, dict[str, Any]]] = [
    (None, {}),
    (EDIT_STATE, EDIT_DATA),
    *[(state, ADD_DATA) for state in ADD_STATES],
]


def _assert_nothing_written_or_fetched(service: MagicMock) -> None:
    service.add_tracking.assert_not_called()
    service.preview.assert_not_called()
    service.change_alert_target.assert_not_called()
    service.find_tracked_product.assert_not_called()


@pytest.mark.asyncio
async def test_press_edits_the_card_into_the_price_prompt_with_the_price_from_the_db(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    card_service.get_product_card.assert_awaited_once_with(_USER, PRODUCT_ID)
    assert card_dialog.sent == []
    assert len(card_dialog.edits) == 1
    edit = card_dialog.edits[0]
    assert (edit.chat_id, edit.message_id) == (_CHAT, _CARD_ID)
    assert edit.text == texts.product_found_text(DB_NAME, DB_PRICE)
    assert f"1{NB}755{NB}₽" in edit.text
    assert DB_NAME in edit.text
    assert edit.parse_mode is None
    assert rows(edit) == [[("Отмена", CB_ADD_CANCEL)]]
    assert await card_dialog.state() == "AddProduct:waiting_target_price"
    assert await card_dialog.data() == _DRAFT_FROM_DB
    _assert_nothing_written_or_fetched(card_service)


@pytest.mark.asyncio
async def test_press_answers_the_callback_exactly_once(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    assert [type(r) for r in card_dialog.requests] == [
        EditMessageText,
        AnswerCallbackQuery,
    ]
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (None, None)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "data"), _START_STATES)
async def test_press_replaces_whatever_dialog_was_running_and_drops_its_draft(
    card_dialog: _Dialog,
    card_service: MagicMock,
    state: str | None,
    data: dict[str, Any],
) -> None:
    await card_dialog.set_state(state, dict(data))

    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    assert await card_dialog.state() == "AddProduct:waiting_target_price"
    assert await card_dialog.data() == _DRAFT_FROM_DB  # no leftover keys


@pytest.mark.asyncio
async def test_amount_then_confirm_adds_the_threshold_with_the_data_from_the_db(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.add_tracking.return_value.direction = AlertDirection.below
    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    await card_dialog.say("1500")

    confirmation = card_dialog.sent[0]
    assert callbacks(confirmation) == [CB_ADD_CONFIRM, CB_ADD_CANCEL]
    assert DB_NAME in confirmation.text
    assert await card_dialog.state() == "AddProduct:confirming"

    await card_dialog.press(CB_ADD_CONFIRM, message_id=sent_message_id(0))

    card_service.add_tracking.assert_awaited_once_with(
        _USER,
        Marketplace.wb,
        DB_ARTICLE,
        DB_NAME,
        DB_PRICE,
        150_000,
        price_fetched_at=DB_FETCHED_AT,
    )
    card_service.preview.assert_not_called()
    assert await card_dialog.state() is None


@pytest.mark.asyncio
async def test_press_on_a_product_without_a_stamp_overwrites_the_foreign_one_with_none(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(price_fetched_at=None)
    await card_dialog.set_state(
        "AddProduct:confirming",
        {**ADD_DATA, "price_fetched_at": "2020-01-02T03:04:05+00:00"},
    )

    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    assert await card_dialog.data() == {**_DRAFT_FROM_DB, "price_fetched_at": None}


@pytest.mark.asyncio
async def test_null_stamp_of_the_product_reaches_add_tracking_as_none(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(price_fetched_at=None)
    card_service.add_tracking.return_value.direction = AlertDirection.below
    await card_dialog.press(f"aadd:{PRODUCT_ID}")
    await card_dialog.say("1500")

    await card_dialog.press(CB_ADD_CONFIRM, message_id=sent_message_id(0))

    assert card_service.add_tracking.await_args.kwargs == {"price_fetched_at": None}


@pytest.mark.asyncio
async def test_invalid_amount_after_the_button_keeps_the_step_with_cancel(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    await card_dialog.say("не число")

    assert [m.text for m in card_dialog.sent] == [texts.ADD_PRICE_INVALID_TEXT]
    assert callbacks(card_dialog.sent[0]) == [CB_ADD_CANCEL]
    assert await card_dialog.state() == "AddProduct:waiting_target_price"
    assert await card_dialog.data() == _DRAFT_FROM_DB


@pytest.mark.asyncio
async def test_amount_equal_to_the_price_from_the_db_is_refused_and_keeps_the_step(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    await card_dialog.say("1755")

    assert [m.text for m in card_dialog.sent] == [texts.ADD_TARGET_EQUALS_TEXT]
    assert await card_dialog.state() == "AddProduct:waiting_target_price"


@pytest.mark.asyncio
async def test_product_limit_after_the_button_is_reported_and_clears_the_dialog(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.add_tracking.side_effect = ProductLimitExceededError(37)
    await card_dialog.press(f"aadd:{PRODUCT_ID}")
    await card_dialog.say("1500")

    await card_dialog.press(CB_ADD_CONFIRM, message_id=sent_message_id(0))

    assert card_dialog.sent[-1].text == texts.limit_exceeded_text(37)
    assert await card_dialog.state() is None


@pytest.mark.asyncio
async def test_missing_or_foreign_product_says_it_is_gone_and_keeps_the_running_dialog(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    """The card read is scoped to the pressing user: a forged id yields no card."""
    card_service.get_product_card.return_value = None
    await card_dialog.set_state(EDIT_STATE, dict(EDIT_DATA))

    await card_dialog.press("aadd:777")

    card_service.get_product_card.assert_awaited_once_with(_USER, 777)
    assert [(e.text, e.parse_mode) for e in card_dialog.edits] == [
        (texts.PRODUCT_ALREADY_GONE_TEXT, None)
    ]
    assert rows(card_dialog.edits[0]) == [CARD_NAV]
    assert len(card_dialog.callback_answers) == 1
    assert await card_dialog.state() == EDIT_STATE
    assert await card_dialog.data() == EDIT_DATA
    _assert_nothing_written_or_fetched(card_service)


@pytest.mark.asyncio
async def test_press_without_a_message_pops_up_stale_and_changes_nothing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(f"aadd:{PRODUCT_ID}", with_message=False)

    card_service.get_product_card.assert_not_called()
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    assert await card_dialog.state() is None


@pytest.mark.asyncio
async def test_press_on_an_inaccessible_message_sends_the_prompt_as_a_new_message(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press(f"aadd:{PRODUCT_ID}", accessible=False)

    assert card_dialog.edits == []
    assert [m.text for m in card_dialog.sent] == [
        texts.product_found_text(DB_NAME, DB_PRICE)
    ]
    assert len(card_dialog.callback_answers) == 1
    assert await card_dialog.state() == "AddProduct:waiting_target_price"


@pytest.mark.asyncio
async def test_press_with_a_refused_edit_sends_the_prompt_as_a_new_message(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_dialog.faults.edit_error = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: can't be edited"
    )

    await card_dialog.press(f"aadd:{PRODUCT_ID}")

    assert [type(m) for m in card_dialog.sent] == [SendMessage]
    assert card_dialog.sent[0].text == texts.product_found_text(DB_NAME, DB_PRICE)
    assert len(card_dialog.callback_answers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    ["aadd", "aadd:", "aadd:0", "aadd:-1", "aadd:x", "aadd:2147483648", "aadd:5:6"],
)
async def test_broken_add_payload_gets_the_stale_reply_and_never_reaches_the_service(
    card_dialog: _Dialog, card_service: MagicMock, payload: str
) -> None:
    await card_dialog.set_state(EDIT_STATE, dict(EDIT_DATA))

    await card_dialog.press(payload)

    card_service.get_product_card.assert_not_called()
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    assert await card_dialog.state() == EDIT_STATE
    assert await card_dialog.data() == EDIT_DATA
