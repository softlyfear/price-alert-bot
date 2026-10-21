"""Keyboard under a price notification (PAB-097 AC1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.bot import texts
from app.bot.keyboards import CB_LIST_PREFIXES
from app.bot.keyboards import CB_NOTICE_MENU
from app.bot.keyboards import NoticeCardCb
from app.bot.keyboards import notice_keyboard

_MAX_ID = 2_147_483_647


def test_notice_keyboard_is_open_product_row_then_main_menu_row() -> None:
    rows = notice_keyboard(42).inline_keyboard

    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [("Открыть товар", "ncard:42")],
        [("Главное меню", "notice:menu")],
    ]
    assert texts.MENU_MAIN_BUTTON_TEXT == "Главное меню"
    assert CB_NOTICE_MENU == "notice:menu"


def test_notice_menu_callback_is_not_under_the_menu_prefix() -> None:
    """Otherwise ``on_unknown_menu`` would answer it with a stale popup."""
    assert not CB_NOTICE_MENU.startswith("menu:")


def test_notice_prefix_is_a_list_prefix_so_a_broken_payload_gets_the_stale_reply() -> (
    None
):
    assert "ncard" in CB_LIST_PREFIXES


def test_notice_payload_at_the_largest_id_fits_the_telegram_limit() -> None:
    packed = NoticeCardCb(product_id=_MAX_ID).pack()

    assert packed == f"ncard:{_MAX_ID}"
    assert len(packed.encode()) <= 64
    assert notice_keyboard(_MAX_ID).inline_keyboard[0][0].callback_data == packed


@pytest.mark.parametrize("bad", [0, -1, _MAX_ID + 1])
def test_notice_callback_rejects_ids_outside_the_int4_range(bad: int) -> None:
    with pytest.raises(ValidationError):
        NoticeCardCb(product_id=bad)


@pytest.mark.parametrize("good", [1, _MAX_ID])
def test_notice_callback_accepts_the_range_boundaries(good: int) -> None:
    assert NoticeCardCb(product_id=good).product_id == good
