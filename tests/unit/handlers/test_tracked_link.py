"""A repeated link to an already tracked product shows its card (PAB-096).

Both entries of the link step go through ``Dispatcher.feed_update``: a link
typed inside ``/add`` and the "add this product" button. The replaced boundary
is ``TrackingService`` (``card_service`` fixture); the lookup is a coroutine
whose side effect moves the FSM mid-lookup to reproduce the PAB-091 races
without a sleep.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage

from app.bot import texts
from app.bot.states import AddProduct
from app.models.enums import Marketplace
from app.schemas.marketplace import MarketplaceProductData
from app.services.tracking import ProductCard
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import CARD_NAV
from tests.unit.handlers.card_dialog_support import CARD_TEXT
from tests.unit.handlers.card_dialog_support import DB_ARTICLE
from tests.unit.handlers.card_dialog_support import NB
from tests.unit.handlers.card_dialog_support import WB_LINK
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import rows

_REF_PAYLOAD = f"addref:wb:{DB_ARTICLE}"
_ALREADY = "Товар уже отслеживается"
_EXPECTED_ROWS = [
    [(f"Изменить порог: 1{NB}500{NB}₽", "aedit:11:5")],
    [(f"Убрать порог: 1{NB}500{NB}₽", "adel:11:5")],
    [("➕ Добавить порог", "aadd:5")],
    [("Перестать следить", "pdel:5")],
    CARD_NAV,
]
_Mover = Callable[[_Dialog], Awaitable[None]]


async def _new_add(dialog: _Dialog) -> None:
    """A newer /add: waiting for a link with another marker."""
    await dialog.set_state(AddProduct.waiting_link.state, {"link_message_id": 999})


async def _cancelled(dialog: _Dialog) -> None:
    await dialog.set_state(None)


async def _moved_on(dialog: _Dialog) -> None:
    await dialog.set_state(
        AddProduct.waiting_target_price.state, {"article": 1, "link_message_id": 1}
    )


_AFTER_MOVE: dict[str, tuple[str | None, dict[str, Any]]] = {
    "_new_add": (AddProduct.waiting_link.state, {"link_message_id": 999}),
    "_cancelled": (None, {}),
    "_moved_on": (
        AddProduct.waiting_target_price.state,
        {"article": 1, "link_message_id": 1},
    ),
}
_MOVERS = [
    pytest.param(_new_add, id="newer-link-or-add"),
    pytest.param(_cancelled, id="cancel"),
    pytest.param(_moved_on, id="state-moved-on"),
]


def _move_during_lookup(service: MagicMock, dialog: _Dialog, mover: _Mover) -> None:
    async def lookup(*_args: Any) -> ProductCard:
        await mover(dialog)
        return card()

    service.find_tracked_product.side_effect = lookup


# --- entry 1: a link typed inside /add ----------------------------------------------


@pytest.mark.asyncio
async def test_link_to_a_tracked_product_sends_one_card_message_and_ends_the_dialog(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.command("add")
    base = len(card_dialog.requests)

    await card_dialog.say(WB_LINK)

    step = card_dialog.requests[base:]
    assert [type(r) for r in step] == [SendMessage]
    shown = step[0]
    assert isinstance(shown, SendMessage)
    assert shown.chat_id == _CHAT
    assert shown.text == f"{_ALREADY}\n\n{CARD_TEXT}"
    assert shown.parse_mode is None
    assert rows(shown) == _EXPECTED_ROWS
    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}


@pytest.mark.asyncio
async def test_link_to_a_tracked_product_never_asks_the_marketplace_or_says_checking(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.command("add")

    await card_dialog.say(WB_LINK)

    card_service.find_tracked_product.assert_awaited_once_with(
        _USER, Marketplace.wb, DB_ARTICLE
    )
    card_service.preview.assert_not_called()
    card_service.add_tracking.assert_not_called()
    shown = [
        r.text
        for r in card_dialog.requests
        if isinstance(r, (SendMessage, EditMessageText))
    ]
    assert texts.ADD_CHECKING_TEXT not in shown
    assert not any(isinstance(r, EditMessageText) for r in card_dialog.requests)


@pytest.mark.asyncio
async def test_link_to_an_untracked_product_goes_on_to_the_check(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.find_tracked_product.return_value = None
    card_service.preview.return_value = MarketplaceProductData(name="Товар", price=100)
    await card_dialog.command("add")

    await card_dialog.say(WB_LINK)

    card_service.preview.assert_awaited_once_with(_USER, Marketplace.wb, DB_ARTICLE)
    assert texts.ADD_CHECKING_TEXT in [m.text for m in card_dialog.sent]


@pytest.mark.asyncio
@pytest.mark.parametrize("mover", _MOVERS)
async def test_link_lookup_overtaken_shows_nothing_and_keeps_the_new_state(
    card_dialog: _Dialog, card_service: MagicMock, mover: _Mover
) -> None:
    await card_dialog.command("add")
    _move_during_lookup(card_service, card_dialog, mover)
    base = len(card_dialog.requests)

    await card_dialog.say(WB_LINK)
    state, data = await card_dialog.state(), await card_dialog.data()

    assert card_dialog.requests[base:] == []
    card_service.preview.assert_not_called()
    # Whatever the mover wrote must still be there: no clear() by the late lookup.
    assert (state, data) == _AFTER_MOVE[mover.__name__]


# --- entry 2: the "add this product" button ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [None, AddProduct.waiting_link.state])
async def test_add_button_on_a_tracked_product_edits_the_message_into_the_card(
    card_dialog: _Dialog, card_service: MagicMock, start: str | None
) -> None:
    await card_dialog.set_state(start, {"link_message_id": 3} if start else {})

    await card_dialog.press(_REF_PAYLOAD)

    assert [type(r) for r in card_dialog.requests] == [
        EditMessageText,
        AnswerCallbackQuery,
    ]
    edit = card_dialog.edits[0]
    assert (edit.chat_id, edit.message_id) == (_CHAT, _CARD_ID)
    assert edit.text == f"{_ALREADY}\n\n{CARD_TEXT}"
    assert edit.parse_mode is None
    assert rows(edit) == _EXPECTED_ROWS
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (None, None)
    ]
    card_service.find_tracked_product.assert_awaited_once_with(
        _USER, Marketplace.wb, DB_ARTICLE
    )
    card_service.preview.assert_not_called()
    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}


@pytest.mark.asyncio
async def test_add_button_on_a_tracked_product_with_a_refused_edit_sends_one_new_card(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_dialog.faults.edit_error = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: can't be edited"
    )

    await card_dialog.press(_REF_PAYLOAD)

    assert [m.text for m in card_dialog.sent] == [f"{_ALREADY}\n\n{CARD_TEXT}"]
    assert rows(card_dialog.sent[0]) == _EXPECTED_ROWS
    assert len(card_dialog.callback_answers) == 1
    assert await card_dialog.state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mover", _MOVERS)
async def test_add_button_lookup_overtaken_gives_one_popup_and_keeps_the_new_state(
    card_dialog: _Dialog, card_service: MagicMock, mover: _Mover
) -> None:
    _move_during_lookup(card_service, card_dialog, mover)

    await card_dialog.press(_REF_PAYLOAD)
    state, data = await card_dialog.state(), await card_dialog.data()

    assert [type(r) for r in card_dialog.requests] == [AnswerCallbackQuery]
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.ADD_STALE_ALERT_TEXT, True)
    ]
    card_service.preview.assert_not_called()
    assert (state, data) == _AFTER_MOVE[mover.__name__]


@pytest.mark.asyncio
async def test_add_button_on_an_untracked_product_still_checks_the_marketplace(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.find_tracked_product.return_value = None
    card_service.preview.return_value = MarketplaceProductData(name="Товар", price=100)

    await card_dialog.press(_REF_PAYLOAD)

    card_service.preview.assert_awaited_once_with(_USER, Marketplace.wb, DB_ARTICLE)
    assert card_dialog.edits[0].text == texts.ADD_CHECKING_TEXT
