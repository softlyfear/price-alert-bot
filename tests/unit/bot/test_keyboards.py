"""Tests for app.bot.keyboards and app.bot.states (PAB-068 AC6)."""

from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import cancel_keyboard
from app.bot.keyboards import confirm_keyboard
from app.bot.states import AddProduct


def test_cancel_keyboard_is_a_single_cancel_button() -> None:
    rows = cancel_keyboard().inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(texts.CANCEL_BUTTON_TEXT, CB_ADD_CANCEL)]
    ]


def test_confirm_keyboard_has_confirm_then_cancel_in_one_row() -> None:
    rows = confirm_keyboard().inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [
            (texts.CONFIRM_BUTTON_TEXT, CB_ADD_CONFIRM),
            (texts.CANCEL_BUTTON_TEXT, CB_ADD_CANCEL),
        ]
    ]


def test_button_labels_are_the_literals_users_see() -> None:
    assert texts.CONFIRM_BUTTON_TEXT == "Подтвердить"
    assert texts.CANCEL_BUTTON_TEXT == "Отмена"
    assert CB_ADD_CONFIRM != CB_ADD_CANCEL


def test_add_product_states_are_the_three_dialog_steps() -> None:
    assert AddProduct.waiting_link.state == "AddProduct:waiting_link"
    assert AddProduct.waiting_target_price.state == "AddProduct:waiting_target_price"
    assert AddProduct.confirming.state == "AddProduct:confirming"
