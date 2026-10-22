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

from app.bot import texts
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import NB
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import product

_PRICES_LINE = "\nЦены на "


def _at(hour: int, minute: int = 0, *, day: int = 21) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _price_line(text: str) -> str | None:
    lines = [line for line in text.split("\n") if line.startswith("Цены на ")]
    assert len(lines) <= 1
    return lines[0] if lines else None


@pytest.mark.asyncio
async def test_list_label_is_the_oldest_price_among_the_shown_products(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_products.return_value = [
        product(1, _at(11, 5)),
        product(2, _at(9, 0)),  # the oldest: 12:00 in Moscow
        product(3, _at(11, 59)),
    ]

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 12:00"


@pytest.mark.asyncio
async def test_list_label_does_not_depend_on_the_order_of_the_products(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_products.return_value = [
        product(1, _at(9, 0)),
        product(2, _at(11, 5)),
    ]

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 12:00"


@pytest.mark.asyncio
async def test_list_label_adds_the_date_when_the_oldest_price_is_not_from_today(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_products.return_value = [
        product(1, _at(11, 5)),
        product(2, _at(21, 30, day=20)),  # 00:30 on the 21st in Moscow, still older
        product(3, _at(23, 30, day=19)),  # 02:30 on the 20th in Moscow: the oldest
    ]

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 20.10 02:30"


@pytest.mark.asyncio
async def test_list_has_no_label_when_any_shown_product_has_no_stamp(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_products.return_value = [
        product(1, _at(11, 5)),
        product(2, None),
        product(3, _at(11, 5)),
    ]

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) is None
    assert _PRICES_LINE not in card_dialog.sent[0].text


@pytest.mark.asyncio
async def test_list_label_ignores_a_product_beyond_the_fifty_shown_ones(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    shown = [product(i, _at(11, 5)) for i in range(1, 51)]
    card_service.list_products.return_value = [*shown, product(51, _at(6, 0))]

    await card_dialog.command("list")

    assert "Показаны первые 50 из 51." in card_dialog.sent[0].text
    assert _price_line(card_dialog.sent[0].text) == "Цены на 14:05"


@pytest.mark.asyncio
async def test_list_keeps_its_label_when_only_a_hidden_product_has_no_stamp(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    shown = [product(i, _at(11, 5)) for i in range(1, 51)]
    card_service.list_products.return_value = [*shown, product(51, None)]

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 14:05"


@pytest.mark.asyncio
async def test_list_has_no_label_when_the_stamp_of_the_fiftieth_product_is_missing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    products = [product(i, _at(11, 5)) for i in range(1, 50)]
    products.append(product(50, None))
    products.append(product(51, _at(11, 5)))
    card_service.list_products.return_value = products

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) is None


@pytest.mark.asyncio
async def test_list_label_uses_the_display_zone_of_the_dispatcher(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_dialog.dispatcher["display_timezone"] = ZoneInfo("Asia/Novosibirsk")
    card_service.list_products.return_value = [product(1, _at(11, 5))]

    await card_dialog.command("list")

    assert _price_line(card_dialog.sent[0].text) == "Цены на 18:05"


@pytest.mark.asyncio
async def test_list_label_is_also_on_the_menu_button_screen(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_products.return_value = [product(1, _at(11, 5))]

    await card_dialog.press("menu:list")

    assert _price_line(card_dialog.edits[0].text) == "Цены на 14:05"


@pytest.mark.asyncio
async def test_card_shows_the_price_time_in_brackets_after_the_price(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(_at(11, 5))

    await card_dialog.press("pcard:5")

    assert f"\nТекущая цена: 1{NB}755{NB}₽ (цена на 14:05)\n" in (
        card_dialog.edits[0].text
    )


@pytest.mark.asyncio
async def test_card_adds_the_date_when_the_price_is_not_from_today(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(_at(11, 5, day=20))

    await card_dialog.press("pcard:5")

    assert "(цена на 20.10 14:05)" in card_dialog.edits[0].text


@pytest.mark.asyncio
async def test_card_without_a_stamp_has_no_brackets(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(None)

    await card_dialog.press("pcard:5")

    assert f"\nТекущая цена: 1{NB}755{NB}₽\n" in card_dialog.edits[0].text
    assert "цена на" not in card_dialog.edits[0].text


@pytest.mark.asyncio
async def test_card_label_uses_the_display_zone_of_the_dispatcher(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_dialog.dispatcher["display_timezone"] = ZoneInfo("Asia/Novosibirsk")
    card_service.get_product_card.return_value = card(_at(11, 5))

    await card_dialog.press("pcard:5")

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
async def test_stop_tracking_button_needs_no_display_zone_in_the_dispatcher(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    del card_dialog.dispatcher.workflow_data["display_timezone"]
    card_service.remove_product.return_value = True

    await card_dialog.press("pdel:5")

    assert [e.text for e in card_dialog.edits] == [texts.LIST_PRODUCT_REMOVED_TEXT]
