"""The "price at HH:MM" labels of the list and the card screens (PAB-100 AC9).

The clock of both screens is frozen to 2026-10-21 12:00 UTC by the autouse
fixture of ``conftest.py``; the display zone is the one in the dispatcher data,
so a test can swap it. Moscow is UTC+3, Novosibirsk UTC+7.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage

from app.bot import texts
from app.models.enums import ProductFailure
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import NB
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import page_of
from tests.unit.handlers.card_dialog_support import product
from tests.unit.handlers.card_dialog_support import rows

_PRICES_LINE = "\nЦены на "


def _at(hour: int, minute: int = 0, *, day: int = 21, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def _product_buttons(
    request: SendMessage | EditMessageText,
) -> list[tuple[str, str | None]]:
    """Product buttons of a rendered list page, without the action rows."""
    return [
        row[0]
        for row in rows(request)
        if row[0][1] is not None and row[0][1].startswith("pcard:")
    ]


def _price_line(text: str) -> str | None:
    lines = [line for line in text.split("\n") if line.startswith("Цены на ")]
    assert len(lines) <= 1
    return lines[0] if lines else None


@pytest.mark.asyncio
async def test_list_label_is_the_freshest_price_among_the_shown_products(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of(
        [
            product(1, _at(11, 5)),
            product(2, _at(9, 0)),
            product(3, _at(11, 59)),  # the freshest: 14:59 in Moscow
        ]
    )

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 14:59"


@pytest.mark.asyncio
async def test_list_label_does_not_depend_on_the_order_of_the_products(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of(
        [
            product(1, _at(11, 59)),
            product(2, _at(9, 0)),
        ]
    )

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 14:59"


@pytest.mark.asyncio
async def test_list_label_adds_the_date_when_the_freshest_price_is_not_from_today(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of(
        [
            product(1, _at(10, 0, day=19)),
            product(2, _at(10, 0, day=20)),  # the freshest: 13:00 on the 20th in Moscow
        ]
    )

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 20.10 13:00"


@pytest.mark.asyncio
async def test_list_has_no_label_when_any_shown_product_has_no_stamp(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of(
        [
            product(1, _at(11, 5)),
            product(2, None),
            product(3, _at(11, 5)),
        ]
    )

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) is None
    assert _PRICES_LINE not in card_dialog.sent[0].text


@pytest.mark.asyncio
async def test_list_label_is_taken_from_the_products_of_the_shown_page(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    shown = [product(i, _at(11, 5)) for i in range(11, 21)]
    shown[3] = product(14, _at(11, 30))  # the freshest of the page: 14:30 in Moscow
    card_service.list_page.return_value = page_of(shown, page=2, pages=3, total=23)

    await card_dialog.command("list")

    assert "Стр. 2/3." in card_dialog.sent[0].text
    assert _price_line(card_dialog.sent[0].text) == "Цены на 14:30"


@pytest.mark.asyncio
async def test_list_marks_a_price_that_missed_two_scheduler_cycles(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    """The clock is 12:00 UTC and the interval is 900 s: the cutoff is 11:30."""
    card_service.list_page.return_value = page_of(
        [
            product(1, _at(11, 29, second=59)),  # 1801 s old: stale
            product(2, _at(11, 30)),  # exactly 1800 s old: still fresh
        ]
    )

    await card_dialog.command("list")

    buttons = _product_buttons(card_dialog.sent[0])
    assert buttons[0] == (
        f"{texts.STALE_PRICE_MARKER} · 1{NB}755{NB}₽ · Из базы <b>1",
        "pcard:1",
    )
    assert buttons[1] == (f"1{NB}755{NB}₽ · Из базы <b>1", "pcard:2")


@pytest.mark.asyncio
async def test_list_marks_nothing_when_the_prices_came_from_one_pass(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of(
        [product(1, _at(11, 59, second=30)), product(2, _at(11, 59, second=50))]
    )

    await card_dialog.command("list")

    buttons = _product_buttons(card_dialog.sent[0])
    assert [text for text, _ in buttons] == [
        f"1{NB}755{NB}₽ · Из базы <b>1",
        f"1{NB}755{NB}₽ · Из базы <b>1",
    ]


@pytest.mark.asyncio
async def test_list_marker_uses_the_scheduler_interval_of_the_dispatcher(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_dialog.dispatcher["scheduler_interval_seconds"] = 30
    card_service.list_page.return_value = page_of([product(1, _at(11, 58))])

    await card_dialog.command("list")

    assert rows(card_dialog.sent[0])[0][0][0].startswith(texts.STALE_PRICE_MARKER)


@pytest.mark.asyncio
async def test_list_does_not_mark_a_product_without_a_price_stamp(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of([product(1, None)])

    await card_dialog.command("list")

    assert rows(card_dialog.sent[0])[0][0] == (
        f"1{NB}755{NB}₽ · Из базы <b>1",
        "pcard:1",
    )


@pytest.mark.asyncio
async def test_list_marks_a_stale_out_of_stock_product_after_the_marker(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    stale = product(1, _at(10, 0))
    stale.last_failure = ProductFailure.out_of_stock
    card_service.list_page.return_value = page_of([stale])

    await card_dialog.command("list")

    assert rows(card_dialog.sent[0])[0][0] == (
        f"{texts.STALE_PRICE_MARKER} · Нет в наличии · 1{NB}755{NB}₽ · Из базы <b>1",
        "pcard:1",
    )


@pytest.mark.asyncio
async def test_list_has_no_label_when_the_stamp_of_the_last_page_product_is_missing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    products = [product(i, _at(11, 5)) for i in range(1, 10)]
    products.append(product(10, None))
    card_service.list_page.return_value = page_of(products)

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) is None


@pytest.mark.asyncio
async def test_list_label_uses_the_display_zone_of_the_dispatcher(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_dialog.dispatcher["display_timezone"] = ZoneInfo("Asia/Novosibirsk")
    card_service.list_page.return_value = page_of([product(1, _at(11, 5))])

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 18:05"


@pytest.mark.asyncio
async def test_list_label_is_also_on_the_menu_button_screen(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = page_of([product(1, _at(11, 5))])

    await card_dialog.press("menu:list")

    assert card_dialog.edits[0].text is not None
    assert _price_line(card_dialog.edits[0].text) == "Цены на 14:05"


@pytest.mark.asyncio
async def test_card_shows_the_price_time_in_brackets_after_the_price(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(_at(11, 5))

    await card_dialog.press("pcard:5")

    assert card_dialog.edits[0].text is not None
    assert f"\nТекущая цена: 1{NB}755{NB}₽ (цена на 14:05)\n" in (
        card_dialog.edits[0].text
    )


@pytest.mark.asyncio
async def test_card_adds_the_date_when_the_price_is_not_from_today(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(_at(11, 5, day=20))

    await card_dialog.press("pcard:5")

    assert card_dialog.edits[0].text is not None
    assert "(цена на 20.10 14:05)" in card_dialog.edits[0].text


@pytest.mark.asyncio
async def test_card_without_a_stamp_has_no_brackets(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(None)

    await card_dialog.press("pcard:5")

    assert card_dialog.edits[0].text is not None
    assert f"\nТекущая цена: 1{NB}755{NB}₽\n" in card_dialog.edits[0].text
    assert "цена на" not in card_dialog.edits[0].text


@pytest.mark.asyncio
async def test_card_label_uses_the_display_zone_of_the_dispatcher(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_dialog.dispatcher["display_timezone"] = ZoneInfo("Asia/Novosibirsk")
    card_service.get_product_card.return_value = card(_at(11, 5))

    await card_dialog.press("pcard:5")

    assert card_dialog.edits[0].text is not None
    assert "(цена на 18:05)" in card_dialog.edits[0].text


@pytest.mark.asyncio
async def test_add_threshold_button_needs_no_display_zone_in_the_dispatcher(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    del card_dialog.dispatcher.workflow_data["display_timezone"]

    await card_dialog.press("aadd:5")

    assert await card_dialog.state() == "AddProduct:waiting_target_price"
    assert len(card_dialog.edits) == 1


@pytest.mark.asyncio
async def test_stop_tracking_button_draws_the_list_page_with_the_oldest_price_label(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.remove_product.return_value = True
    card_service.list_page_of.return_value = 2
    card_service.list_page.return_value = page_of(
        [product(11, _at(9, 0))], page=2, pages=2, total=11
    )

    await card_dialog.press("pdel:5")

    assert card_dialog.edits[0].text == (
        f"{texts.LIST_PRODUCT_REMOVED_TEXT}\n\n"
        "Вы следите за товарами: 11. Стр. 2/2.\n"
        "Цены на 12:00\n"
        "Цена без учёта персональных скидок."
    )
    card_service.list_page.assert_awaited_once_with(_USER, 2)
