"""Tests for the /list keyboards and callback factories (PAB-070 AC8, AC10, AC11)."""

import pytest
from pydantic import ValidationError

from app.bot import texts
from app.bot.keyboards import CB_LIST_PREFIXES
from app.bot.keyboards import MAX_LIST_BUTTONS
from app.bot.keyboards import AlertAddCb
from app.bot.keyboards import AlertEditCb
from app.bot.keyboards import AlertRemoveCb
from app.bot.keyboards import EditCancelCb
from app.bot.keyboards import ProductCardCb
from app.bot.keyboards import ProductRemoveCb
from app.bot.keyboards import edit_cancel_keyboard
from app.bot.keyboards import nav_keyboard
from app.bot.keyboards import product_card_keyboard
from app.bot.keyboards import product_list_keyboard
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product

_NB = "\u00a0"
_MAX_ID = 2_147_483_647
_CARD_NAV_ROW = [("← К списку", "menu:list"), ("Главное меню", "menu:start")]
_NAV_ROW = [("➕ Добавить товар", "menu:add"), ("Главное меню", "menu:start")]


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
        _NAV_ROW,
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

    assert len(rows) == 50 + 1
    assert rows[49][0].callback_data == "pcard:50"
    assert [(b.text, b.callback_data) for b in rows[-1]] == _NAV_ROW


def test_list_keyboard_with_fifty_one_products_shows_the_first_fifty() -> None:
    rows = product_list_keyboard([_product(i) for i in range(1, 52)]).inline_keyboard

    assert len(rows) == 50 + 1
    assert rows[49][0].callback_data == "pcard:50"
    assert [(b.text, b.callback_data) for b in rows[-1]] == _NAV_ROW


def test_list_keyboard_without_products_is_only_the_navigation_row() -> None:
    rows = product_list_keyboard([]).inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [_NAV_ROW]


def test_card_keyboard_has_edit_then_remove_per_alert_then_stop_tracking_then_nav() -> (
    None
):
    rows = product_card_keyboard(
        4,
        [
            _alert(11, AlertDirection.below, 100_000),
            _alert(12, AlertDirection.above, 300_000),
        ],
    ).inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(f"Изменить порог: 1{_NB}000{_NB}₽", "aedit:11:4")],
        [(f"Убрать порог: 1{_NB}000{_NB}₽", "adel:11:4")],
        [(f"Изменить порог: 3{_NB}000{_NB}₽", "aedit:12:4")],
        [(f"Убрать порог: 3{_NB}000{_NB}₽", "adel:12:4")],
        [(texts.ADD_ALERT_BUTTON_TEXT, "aadd:4")],
        [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:4")],
        _CARD_NAV_ROW,
    ]


def test_card_keyboard_shows_arrows_only_on_buttons_with_a_same_amount_twin() -> None:
    rows = product_card_keyboard(
        4,
        [
            _alert(11, AlertDirection.below, 100_000),
            _alert(12, AlertDirection.above, 100_000),
            _alert(13, AlertDirection.below, 50_000),
        ],
    ).inline_keyboard

    assert [row[0].text for row in rows[:6]] == [
        f"Изменить порог: ↓ 1{_NB}000{_NB}₽",
        f"Убрать порог: ↓ 1{_NB}000{_NB}₽",
        f"Изменить порог: ↑ 1{_NB}000{_NB}₽",
        f"Убрать порог: ↑ 1{_NB}000{_NB}₽",
        f"Изменить порог: 500{_NB}₽",
        f"Убрать порог: 500{_NB}₽",
    ]


def test_card_keyboard_without_alerts_has_stop_tracking_then_nav() -> None:
    rows = product_card_keyboard(4, []).inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(texts.ADD_ALERT_BUTTON_TEXT, "aadd:4")],
        [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:4")],
        _CARD_NAV_ROW,
    ]


def test_card_keyboard_callbacks_fit_the_telegram_limit_at_the_largest_ids() -> None:
    rows = product_card_keyboard(
        _MAX_ID, [_alert(_MAX_ID, AlertDirection.below, 100_000)]
    ).inline_keyboard

    data = [b.callback_data for row in rows for b in row]
    assert data[:2] == [f"aedit:{_MAX_ID}:{_MAX_ID}", f"adel:{_MAX_ID}:{_MAX_ID}"]
    assert all(d is not None and len(d.encode()) <= 64 for d in data)


def test_card_keyboard_puts_each_edit_row_right_before_its_remove_row() -> None:
    alerts = [
        _alert(21, AlertDirection.below, 100_000),
        _alert(22, AlertDirection.above, 300_000),
        _alert(23, AlertDirection.below, 50_000),
    ]

    data = [
        row[0].callback_data
        for row in product_card_keyboard(4, alerts).inline_keyboard[:-3]
    ]

    assert data == [
        "aedit:21:4",
        "adel:21:4",
        "aedit:22:4",
        "adel:22:4",
        "aedit:23:4",
        "adel:23:4",
    ]


def test_edit_cancel_keyboard_is_a_single_cancel_button() -> None:
    rows = edit_cancel_keyboard(5).inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(texts.CANCEL_BUTTON_TEXT, "ecancel:5")]
    ]


def test_edit_prefix_is_a_list_prefix_so_a_broken_payload_gets_the_stale_reply() -> (
    None
):
    assert "aedit" in CB_LIST_PREFIXES


def test_nav_keyboard_is_exactly_the_card_navigation_row() -> None:
    rows = nav_keyboard().inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [_CARD_NAV_ROW]


def test_callback_payloads_fit_the_telegram_limit_at_the_largest_id() -> None:
    packed = [
        ProductCardCb(product_id=_MAX_ID).pack(),
        AlertRemoveCb(alert_id=_MAX_ID, product_id=_MAX_ID).pack(),
        AlertEditCb(alert_id=_MAX_ID, product_id=_MAX_ID).pack(),
        ProductRemoveCb(product_id=_MAX_ID).pack(),
    ]

    assert packed == [
        f"pcard:{_MAX_ID}",
        f"adel:{_MAX_ID}:{_MAX_ID}",
        f"aedit:{_MAX_ID}:{_MAX_ID}",
        f"pdel:{_MAX_ID}",
    ]
    assert all(len(p.encode()) <= 64 for p in packed)


@pytest.mark.parametrize("bad", [0, -1, _MAX_ID + 1])
def test_callback_factories_reject_ids_outside_the_int4_range(bad: int) -> None:
    with pytest.raises(ValidationError):
        ProductCardCb(product_id=bad)
    with pytest.raises(ValidationError):
        AlertRemoveCb(alert_id=bad, product_id=1)
    with pytest.raises(ValidationError):
        AlertRemoveCb(alert_id=1, product_id=bad)
    with pytest.raises(ValidationError):
        AlertEditCb(alert_id=bad, product_id=1)
    with pytest.raises(ValidationError):
        AlertEditCb(alert_id=1, product_id=bad)
    with pytest.raises(ValidationError):
        ProductRemoveCb(product_id=bad)


@pytest.mark.parametrize("good", [1, _MAX_ID])
def test_callback_factories_accept_the_range_boundaries(good: int) -> None:
    assert ProductCardCb(product_id=good).product_id == good
    cb = AlertRemoveCb(alert_id=good, product_id=good)
    assert (cb.alert_id, cb.product_id) == (good, good)
    edit = AlertEditCb(alert_id=good, product_id=good)
    assert (edit.alert_id, edit.product_id) == (good, good)
    assert ProductRemoveCb(product_id=good).product_id == good


# --- PAB-096: "Добавить порог" and the typed edit "Отмена" ------------------------


def _flat(product_id: int, alerts: list[Alert]) -> list[tuple[str, str | None]]:
    rows = product_card_keyboard(product_id, alerts).inline_keyboard
    return [(b.text, b.callback_data) for row in rows for b in row]


@pytest.mark.parametrize("count", [0, 1, 3])
def test_card_keyboard_has_exactly_one_add_button_after_the_alert_rows_before_stop(
    count: int,
) -> None:
    alerts = [_alert(30 + i, AlertDirection.below, 100_000 + i) for i in range(count)]

    flat = _flat(4, alerts)

    add = [i for i, (text, _) in enumerate(flat) if text == texts.ADD_ALERT_BUTTON_TEXT]
    stop = [i for i, (_, data) in enumerate(flat) if data == "pdel:4"]
    alert_rows = [
        i for i, (_, data) in enumerate(flat) if (data or "")[:4] in ("aedi", "adel")
    ]
    assert len(add) == 1
    assert flat[add[0]] == ("➕ Добавить порог", "aadd:4")
    assert add[0] == stop[0] - 1
    assert all(i < add[0] for i in alert_rows)
    assert len(alert_rows) == 2 * count


def test_add_and_edit_cancel_payloads_fit_the_telegram_limit_at_the_largest_id() -> (
    None
):
    packed = [
        AlertAddCb(product_id=_MAX_ID).pack(),
        EditCancelCb(product_id=_MAX_ID).pack(),
    ]
    keyboard = edit_cancel_keyboard(_MAX_ID).inline_keyboard

    assert packed == [f"aadd:{_MAX_ID}", f"ecancel:{_MAX_ID}"]
    assert all(len(p.encode()) <= 64 for p in packed)
    assert [[b.callback_data for b in row] for row in keyboard] == [
        [f"ecancel:{_MAX_ID}"]
    ]


@pytest.mark.parametrize("bad", [0, -1, _MAX_ID + 1])
def test_add_and_edit_cancel_factories_reject_ids_outside_the_int4_range(
    bad: int,
) -> None:
    with pytest.raises(ValidationError):
        AlertAddCb(product_id=bad)
    with pytest.raises(ValidationError):
        EditCancelCb(product_id=bad)


@pytest.mark.parametrize("good", [1, _MAX_ID])
def test_add_and_edit_cancel_factories_accept_the_range_boundaries(good: int) -> None:
    assert AlertAddCb(product_id=good).product_id == good
    assert EditCancelCb(product_id=good).product_id == good


def test_add_and_edit_cancel_prefixes_are_list_prefixes_with_the_legacy_one() -> None:
    assert {"aadd", "ecancel", "edit:cancel"} <= set(CB_LIST_PREFIXES)
