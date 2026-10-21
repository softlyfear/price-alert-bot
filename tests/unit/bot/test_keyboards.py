"""Tests for app.bot.keyboards and app.bot.states (PAB-068 AC6)."""

from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import CB_MENU_ADD
from app.bot.keyboards import CB_MENU_ALL
from app.bot.keyboards import CB_MENU_HELP
from app.bot.keyboards import CB_MENU_LIST
from app.bot.keyboards import CB_MENU_PREFIX
from app.bot.keyboards import CB_MENU_START
from app.bot.keyboards import cancel_keyboard
from app.bot.keyboards import confirm_keyboard
from app.bot.keyboards import main_menu_keyboard
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


def test_main_menu_keyboard_is_add_row_then_list_and_help_row() -> None:
    rows = main_menu_keyboard().inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [("➕ Добавить товар", "menu:add")],
        [("Мои товары", "menu:list"), ("Помощь", "menu:help")],
    ]


def test_main_menu_callback_data_are_distinct_prefixed_and_fit_the_limit() -> None:
    values = [CB_MENU_ADD, CB_MENU_LIST, CB_MENU_HELP, CB_MENU_START]

    assert len(set(values)) == 4
    assert frozenset(values) == CB_MENU_ALL
    assert all(v.startswith(CB_MENU_PREFIX) for v in values)
    assert all(len(v.encode()) <= 64 for v in values)


def test_main_menu_has_no_main_menu_button_of_its_own() -> None:
    callbacks = [
        b.callback_data for r in main_menu_keyboard().inline_keyboard for b in r
    ]

    assert CB_MENU_START not in callbacks
