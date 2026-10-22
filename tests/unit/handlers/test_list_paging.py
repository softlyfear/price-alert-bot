"""Handler tests of the paged "My products" list (PAB-108, Р20).

``TrackingService`` is the replaced boundary (a ``MagicMock(spec=...)``);
page arithmetic, clamping and SQL are proven in the service and live tests.
Here: callback validation (the page number is user input), what is drawn for
the page the service returned, the FSM staying untouched and every service
call carrying the pressing user's id (not the chat id).
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from app.bot import texts
from app.services.price_service import RefreshGone
from app.services.price_service import RefreshReport
from app.services.price_service import RefreshThrottled
from app.services.tracking import ProductPage
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import ADD_DATA
from tests.unit.handlers.card_dialog_support import ADD_STATES
from tests.unit.handlers.card_dialog_support import EDIT_DATA
from tests.unit.handlers.card_dialog_support import EDIT_STATE
from tests.unit.handlers.card_dialog_support import PRODUCT_ID
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import page_of
from tests.unit.handlers.card_dialog_support import product
from tests.unit.handlers.card_dialog_support import rows

_STALE_POPUP = (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
_HEADER_P2 = (
    "Вы следите за товарами: 23. Стр. 2/3.\n"
    "Цены на 14:05\n"
    "Цена без учёта персональных скидок."
)


@pytest.fixture
def refresher(card_dialog: _Dialog) -> MagicMock:
    """The ``PriceRefresher`` double of the dispatcher data, with happy defaults."""
    mock = cast(MagicMock, card_dialog.dispatcher["price_refresher"])
    mock.reserve.return_value = None
    mock.refresh_list.return_value = RefreshReport(total=1, updated=1, failures={})
    mock.refresh_product.return_value = RefreshReport(total=1, updated=1, failures={})
    return mock


def _page(number: int, pages: int = 3, total: int = 23) -> ProductPage:
    first = (number - 1) * 10 + 1
    last = min(first + 9, total)
    return page_of(
        [product(i) for i in range(first, last + 1)],
        page=number,
        pages=pages,
        total=total,
    )


def _service_untouched(service: MagicMock, refresher: MagicMock) -> None:
    service.list_page.assert_not_called()
    service.list_page_of.assert_not_called()
    refresher.reserve.assert_not_called()
    refresher.refresh_list.assert_not_called()


# --- the page number is user input ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    ["lpage:0", "lpage:-1", "lpage:10001", "lpage:abc", "lpage:", "lpage:1:2"],
)
async def test_invalid_lpage_payload_gets_the_stale_popup_and_no_service_call(
    card_dialog: _Dialog,
    card_service: MagicMock,
    refresher: MagicMock,
    data: str,
) -> None:
    await card_dialog.press(data)

    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        _STALE_POPUP
    ]
    assert card_dialog.edits == []
    assert card_dialog.sent == []
    _service_untouched(card_service, refresher)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["lref:-1", "lref:0", "lref:10001", "lref:abc"])
async def test_invalid_lref_payload_gets_the_stale_popup_and_refreshes_nothing(
    card_dialog: _Dialog,
    card_service: MagicMock,
    refresher: MagicMock,
    data: str,
) -> None:
    await card_dialog.press(data)

    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        _STALE_POPUP
    ]
    assert card_dialog.edits == []
    _service_untouched(card_service, refresher)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["lpage:1", "lpage:10000"])
async def test_range_boundary_pages_reach_the_service_as_they_are(
    card_dialog: _Dialog, card_service: MagicMock, data: str
) -> None:
    await card_dialog.press(data)

    card_service.list_page.assert_awaited_once_with(_USER, int(data.split(":")[1]))


# --- turning the page ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_lpage_edits_the_message_in_place_into_the_requested_page(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = _page(2)

    await card_dialog.press("lpage:2")

    card_service.list_page.assert_awaited_once_with(_USER, 2)
    assert card_dialog.sent == []
    assert [e.text for e in card_dialog.edits] == [_HEADER_P2]
    assert rows(card_dialog.edits[0])[10:] == [
        [("◀", "lpage:1"), ("▶", "lpage:3")],
        [("🔄 Обновить цены", "lref:2")],
        [("➕ Добавить товар", "menu:add"), ("Главное меню", "menu:start")],
    ]
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (None, None)
    ]


@pytest.mark.asyncio
async def test_lpage_shows_the_page_the_service_clamped_to(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = _page(3)

    await card_dialog.press("lpage:99")

    card_service.list_page.assert_awaited_once_with(_USER, 99)
    assert card_dialog.edits[0].text.startswith("Вы следите за товарами: 23. Стр. 3/3.")
    assert [d for _, d in rows(card_dialog.edits[0])[-3]] == ["lpage:2"]


@pytest.mark.asyncio
async def test_lpage_on_an_inaccessible_message_sends_the_page_as_a_new_message(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = _page(2)

    await card_dialog.press("lpage:2", accessible=False)

    assert card_dialog.edits == []
    assert [(m.chat_id, m.text) for m in card_dialog.sent] == [(_CHAT, _HEADER_P2)]


@pytest.mark.asyncio
async def test_lpage_without_a_message_gives_the_stale_popup_and_sends_nothing(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    await card_dialog.press("lpage:2", with_message=False)

    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        _STALE_POPUP
    ]
    assert card_dialog.sent == []
    assert card_dialog.edits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "data"),
    [
        *[(state, dict(ADD_DATA)) for state in ADD_STATES],
        (EDIT_STATE, dict(EDIT_DATA)),
    ],
)
async def test_lpage_keeps_the_fsm_state_and_draft_in_the_middle_of_a_dialog(
    card_dialog: _Dialog, card_service: MagicMock, state: str, data: dict[str, object]
) -> None:
    card_service.list_page.return_value = _page(2)
    await card_dialog.set_state(state, dict(data))

    await card_dialog.press("lpage:2")

    assert [e.text for e in card_dialog.edits] == [_HEADER_P2]
    assert await card_dialog.state() == state
    assert await card_dialog.data() == data


@pytest.mark.asyncio
async def test_lpage_without_a_dialog_leaves_the_state_empty(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.list_page.return_value = _page(2)

    await card_dialog.press("lpage:2")

    assert await card_dialog.state() is None
    assert await card_dialog.data() == {}


# --- back from the card ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_of_the_15th_of_23_products_leads_back_to_list_page_two(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = card(list_page=2)

    await card_dialog.press(f"pcard:{PRODUCT_ID}")

    assert rows(card_dialog.edits[0])[-1] == [
        ("← К списку", "lpage:2"),
        ("Главное меню", "menu:start"),
    ]


# --- refresh keeps the page ---------------------------------------------------------


@pytest.mark.asyncio
async def test_lref_refreshes_for_the_presser_and_redraws_the_same_page(
    card_dialog: _Dialog,
    card_service: MagicMock,
    refresher: MagicMock,
) -> None:
    card_service.list_page.return_value = _page(2)

    await card_dialog.press("lref:2")

    refresher.reserve.assert_called_once_with(_USER)
    refresher.refresh_list.assert_awaited_once_with(_USER)
    card_service.list_page.assert_awaited_once_with(_USER, 2)
    assert card_dialog.sent == []
    assert [e.text for e in card_dialog.edits] == [
        "Обновляю цены…",
        f"Цены обновлены.\n\n{_HEADER_P2}",
    ]
    assert ("🔄 Обновить цены", "lref:2") in rows(card_dialog.edits[1])[-2]


@pytest.mark.asyncio
async def test_lref_of_an_emptied_page_shows_the_last_non_empty_page(
    card_dialog: _Dialog,
    card_service: MagicMock,
    refresher: MagicMock,
) -> None:
    """Products were deleted meanwhile; the service clamps page 3 to page 2."""
    card_service.list_page.return_value = _page(2, pages=2, total=20)

    await card_dialog.press("lref:3")

    card_service.list_page.assert_awaited_once_with(_USER, 3)
    text = card_dialog.edits[1].text
    assert "Вы следите за товарами: 20. Стр. 2/2." in text
    assert ("🔄 Обновить цены", "lref:2") in rows(card_dialog.edits[1])[-2]


@pytest.mark.asyncio
async def test_lref_throttled_shows_a_popup_and_does_not_touch_the_list(
    card_dialog: _Dialog,
    card_service: MagicMock,
    refresher: MagicMock,
) -> None:
    refresher.reserve.return_value = RefreshThrottled(retry_after_seconds=42)

    await card_dialog.press("lref:2")

    refresher.refresh_list.assert_not_called()
    card_service.list_page.assert_not_called()
    assert card_dialog.edits == []


@pytest.mark.asyncio
async def test_old_pageless_refresh_button_stays_on_page_one(
    card_dialog: _Dialog,
    card_service: MagicMock,
    refresher: MagicMock,
) -> None:
    await card_dialog.press("list:refresh")

    card_service.list_page.assert_awaited_once_with(_USER, 1)


@pytest.mark.asyncio
async def test_card_refresh_of_a_gone_product_redraws_the_page_it_was_on(
    card_dialog: _Dialog,
    card_service: MagicMock,
    refresher: MagicMock,
) -> None:
    refresher.refresh_product.return_value = RefreshGone()
    card_service.list_page_of.return_value = 2
    card_service.list_page.return_value = _page(2)

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    card_service.list_page_of.assert_awaited_once_with(_USER, PRODUCT_ID)
    card_service.list_page.assert_awaited_once_with(_USER, 2)
    assert card_dialog.edits[1].text.endswith(_HEADER_P2)


# --- ownership ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_product_asks_for_the_page_of_the_presser_not_the_chat(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.remove_product.return_value = True

    await card_dialog.press("pdel:23")

    card_service.list_page_of.assert_awaited_once_with(_USER, 23)
    card_service.list_page.assert_awaited_once_with(_USER, 1)
