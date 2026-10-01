"""Tests for the /list keyboards and callback factories (PAB-070 AC8, AC10, AC11)."""

import pytest
from pydantic import ValidationError

from app.bot import texts
from app.bot.keyboards import MAX_LIST_BUTTONS
from app.bot.keyboards import AlertRemoveCb
from app.bot.keyboards import ProductCardCb
from app.bot.keyboards import ProductRemoveCb
from app.bot.keyboards import product_card_keyboard
from app.bot.keyboards import product_list_keyboard
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product

_NB = "\u00a0"
_MAX_ID = 2_147_483_647


def _product(product_id: int, name: str = "Item", price: int = 199_000) -> Product:
    return Product(
        id=product_id,
        user_id=1,
        marketplace=Marketplace.wb,
        article=product_id,
        product_name=name,
        current_price=price,
    )


def _alert(alert_id: int, direction: AlertDirection, target: int) -> Alert:
    return Alert(
        id=alert_id, user_id=1, product_id=1, target_price=target, direction=direction
    )


def test_max_list_buttons_is_fifty() -> None:
    assert MAX_LIST_BUTTONS == 50


def test_list_keyboard_has_one_button_per_product_with_name_and_price() -> None:
    rows = product_list_keyboard(
        [_product(7, "Кроссовки", 199_000), _product(9, "Шапка", 50)]
    ).inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(f"Кроссовки — 1{_NB}990{_NB}₽", "pcard:7")],
        [(f"Шапка — 0,50{_NB}₽", "pcard:9")],
    ]


def test_list_keyboard_cuts_a_long_name_to_forty_characters_with_ellipsis() -> None:
    button = product_list_keyboard([_product(1, "я" * 100)]).inline_keyboard[0][0]

    assert button.text == "я" * 39 + "…" + f" — 1{_NB}990{_NB}₽"


def test_list_keyboard_keeps_a_forty_character_name_intact() -> None:
    button = product_list_keyboard([_product(1, "я" * 40)]).inline_keyboard[0][0]

    assert button.text.startswith("я" * 40 + " — ")


def test_list_keyboard_collapses_whitespace_and_newlines_in_the_name() -> None:
    button = product_list_keyboard([_product(1, "a \n\n  b\tc")]).inline_keyboard[0][0]

    assert button.text.startswith("a b c — ")


def test_list_keyboard_with_exactly_fifty_products_shows_all_of_them() -> None:
    rows = product_list_keyboard([_product(i) for i in range(1, 51)]).inline_keyboard

    assert len(rows) == 50
    assert rows[-1][0].callback_data == "pcard:50"


def test_list_keyboard_with_fifty_one_products_shows_the_first_fifty() -> None:
    rows = product_list_keyboard([_product(i) for i in range(1, 52)]).inline_keyboard

    assert len(rows) == 50
    assert rows[-1][0].callback_data == "pcard:50"


def test_card_keyboard_has_remove_button_per_alert_then_stop_tracking() -> None:
    rows = product_card_keyboard(
        4,
        [
            _alert(11, AlertDirection.below, 100_000),
            _alert(12, AlertDirection.above, 300_000),
        ],
    ).inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(f"Убрать порог: цена станет ниже 1{_NB}000{_NB}₽", "adel:11")],
        [(f"Убрать порог: цена станет выше 3{_NB}000{_NB}₽", "adel:12")],
        [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:4")],
    ]


def test_card_keyboard_without_alerts_has_only_stop_tracking() -> None:
    rows = product_card_keyboard(4, []).inline_keyboard

    assert [[b.callback_data for b in row] for row in rows] == [["pdel:4"]]


def test_callback_payloads_fit_the_telegram_limit_at_the_largest_id() -> None:
    packed = [
        ProductCardCb(product_id=_MAX_ID).pack(),
        AlertRemoveCb(alert_id=_MAX_ID).pack(),
        ProductRemoveCb(product_id=_MAX_ID).pack(),
    ]

    assert packed == [f"pcard:{_MAX_ID}", f"adel:{_MAX_ID}", f"pdel:{_MAX_ID}"]
    assert all(len(p.encode()) <= 64 for p in packed)


@pytest.mark.parametrize("bad", [0, -1, _MAX_ID + 1])
def test_callback_factories_reject_ids_outside_the_int4_range(bad: int) -> None:
    with pytest.raises(ValidationError):
        ProductCardCb(product_id=bad)
    with pytest.raises(ValidationError):
        AlertRemoveCb(alert_id=bad)
    with pytest.raises(ValidationError):
        ProductRemoveCb(product_id=bad)


@pytest.mark.parametrize("good", [1, _MAX_ID])
def test_callback_factories_accept_the_range_boundaries(good: int) -> None:
    assert ProductCardCb(product_id=good).product_id == good
    assert AlertRemoveCb(alert_id=good).alert_id == good
    assert ProductRemoveCb(product_id=good).product_id == good
