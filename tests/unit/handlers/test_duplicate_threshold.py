"""A duplicate threshold keeps the dialog open (PAB-098 AC1, AC2).

Both entries of the add dialog (a link typed after ``/add`` and the card button
"Добавить порог") end in the same ``on_confirm``. The replaced boundary is
``TrackingService`` (``card_service``); the whole path goes through
``Dispatcher.feed_update``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText

from app.bot import texts
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.states import AddProduct
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import ProductLimitExceededError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.schemas.marketplace import MarketplaceProductData
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _NAME
from tests.unit.handlers.add_dialog_support import _PRICE
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import DB_ARTICLE
from tests.unit.handlers.card_dialog_support import DB_NAME
from tests.unit.handlers.card_dialog_support import DB_PRICE
from tests.unit.handlers.card_dialog_support import PRODUCT_ID
from tests.unit.handlers.card_dialog_support import WB_LINK
from tests.unit.handlers.card_dialog_support import rows

_EXIT_ROWS = [
    [("← К списку товаров", "dlg:list")],
    [("Главное меню", "dlg:menu")],
]
_WAITING = AddProduct.waiting_target_price.state
_ADDED = Alert(
    id=1, user_id=1, product_id=1, target_price=1, direction=AlertDirection.below
)

# (name, current price) of the draft per entry: /add takes it from the marketplace
# preview, the card button from the database.
_ENTRIES = [
    pytest.param("add", _NAME, _PRICE, id="add-command"),
    pytest.param("card", DB_NAME, DB_PRICE, id="add-threshold-button"),
]


async def _open_dialog(dialog: _Dialog, service: MagicMock, entry: str) -> None:
    if entry == "add":
        service.find_tracked_product.return_value = None
        service.preview.return_value = MarketplaceProductData(name=_NAME, price=_PRICE)
        await dialog.command("add")
        await dialog.say(WB_LINK)
    else:
        await dialog.press(f"aadd:{PRODUCT_ID}")
    assert await dialog.state() == _WAITING


async def _confirm_card(dialog: _Dialog, amount: str) -> int:
    """Type an amount; return the message id of the new confirmation card."""
    await dialog.say(amount)
    assert await dialog.state() == AddProduct.confirming.state
    card_id = (await dialog.data())["confirm_message_id"]
    assert isinstance(card_id, int)
    return card_id


async def _at_duplicate(dialog: _Dialog, service: MagicMock, entry: str) -> int:
    """Run the dialog up to the duplicate screen; return the card id it edited."""
    service.add_tracking.side_effect = [DuplicateAlertError(), _ADDED]
    await _open_dialog(dialog, service, entry)
    card_id = await _confirm_card(dialog, "1500")
    await dialog.press(CB_ADD_CONFIRM, message_id=card_id)
    return card_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry", "name", "price"), _ENTRIES)
async def test_duplicate_edits_the_confirmation_into_the_exit_screen(
    card_dialog: _Dialog, card_service: MagicMock, entry: str, name: str, price: int
) -> None:
    card_id = await _at_duplicate(card_dialog, card_service, entry)

    edit = card_dialog.edits[-1]
    assert (edit.chat_id, edit.message_id) == (_CHAT, card_id)
    assert edit.text == (
        "У вас уже есть такой порог. Введите другую сумму в рублях, "
        "например 1990 или 1990,50."
    )
    assert edit.parse_mode is None
    assert rows(edit) == _EXIT_ROWS
    assert card_dialog.requests[-2:] == [edit, card_dialog.callback_answers[-1]]


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry", "name", "price"), _ENTRIES)
async def test_duplicate_keeps_the_amount_step_and_the_draft_of_the_product(
    card_dialog: _Dialog, card_service: MagicMock, entry: str, name: str, price: int
) -> None:
    await _at_duplicate(card_dialog, card_service, entry)

    assert await card_dialog.state() == _WAITING
    data = await card_dialog.data()
    assert {
        key: data[key]
        for key in ("marketplace", "article", "product_name", "current_price")
    } == {
        "marketplace": "wb",
        "article": DB_ARTICLE,
        "product_name": name,
        "current_price": price,
    }
    assert len(card_service.add_tracking.await_args_list) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry", "name", "price"), _ENTRIES)
async def test_duplicate_answers_the_callback_once_without_a_popup(
    card_dialog: _Dialog, card_service: MagicMock, entry: str, name: str, price: int
) -> None:
    await _at_duplicate(card_dialog, card_service, entry)

    answers = [r for r in card_dialog.requests if isinstance(r, AnswerCallbackQuery)]
    assert (answers[-1].text, answers[-1].show_alert) == (None, None)
    assert isinstance(card_dialog.requests[-2], EditMessageText)


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry", "name", "price"), _ENTRIES)
async def test_new_amount_after_a_duplicate_is_added_with_that_amount(
    card_dialog: _Dialog, card_service: MagicMock, entry: str, name: str, price: int
) -> None:
    await _at_duplicate(card_dialog, card_service, entry)

    new_card_id = await _confirm_card(card_dialog, "1200")
    await card_dialog.press(CB_ADD_CONFIRM, message_id=new_card_id)

    assert [c.args for c in card_service.add_tracking.await_args_list] == [
        (_USER, Marketplace.wb, DB_ARTICLE, name, price, 150_000),
        (_USER, Marketplace.wb, DB_ARTICLE, name, price, 120_000),
    ]
    assert await card_dialog.state() is None
    assert card_dialog.sent[-1].text.startswith("Отслеживание добавлено")


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry", "name", "price"), _ENTRIES)
async def test_second_duplicate_in_a_row_keeps_the_dialog_open_again(
    card_dialog: _Dialog, card_service: MagicMock, entry: str, name: str, price: int
) -> None:
    await _at_duplicate(card_dialog, card_service, entry)
    card_service.add_tracking.side_effect = DuplicateAlertError()

    new_card_id = await _confirm_card(card_dialog, "1200")
    await card_dialog.press(CB_ADD_CONFIRM, message_id=new_card_id)

    assert card_dialog.edits[-1].message_id == new_card_id
    assert card_dialog.edits[-1].text == texts.ALERT_DUPLICATE_TEXT
    assert await card_dialog.state() == _WAITING


@pytest.mark.asyncio
async def test_product_limit_still_ends_the_dialog_with_the_main_menu(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.add_tracking.side_effect = ProductLimitExceededError(37)
    await _open_dialog(card_dialog, card_service, "add")
    card_id = await _confirm_card(card_dialog, "1500")

    await card_dialog.press(CB_ADD_CONFIRM, message_id=card_id)

    assert card_dialog.sent[-1].text == texts.limit_exceeded_text(37)
    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}


@pytest.mark.asyncio
async def test_duplicate_screen_is_not_shown_to_a_stale_confirmation_card(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.add_tracking.side_effect = DuplicateAlertError()
    await _open_dialog(card_dialog, card_service, "add")
    await _confirm_card(card_dialog, "1500")
    before: dict[str, Any] = await card_dialog.data()

    await card_dialog.press(
        CB_ADD_CONFIRM, message_id=before["confirm_message_id"] + 99
    )

    card_service.add_tracking.assert_not_called()
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.ADD_OLD_CARD_ALERT_TEXT, True)
    ]
    assert await card_dialog.data() == before
