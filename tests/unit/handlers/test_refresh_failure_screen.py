"""A crash after the "Обновляю цены…" screen never leaves it hanging (PAB-100 AC8).

The progress screen has no buttons, so a bare exception would strand the user on
it. Every step after it (the refresh, the list read, the card read, the render of
the "gone" screen) is covered here: the screen is replaced with the generic error
and the navigation row, the exception is logged once and not re-raised (the
``errors`` handler would otherwise send a second copy). Boundaries replaced are
the same as in ``test_refresh_buttons.py``: ``TrackingService`` and
``PriceRefresher`` doubles, and the fake Bot API session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText

from app.bot import texts
from app.services.price_service import RefreshGone
from app.services.price_service import RefreshReport
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import PRODUCT_ID
from tests.unit.handlers.card_dialog_support import page_of
from tests.unit.handlers.card_dialog_support import product
from tests.unit.handlers.card_dialog_support import rows

if TYPE_CHECKING:
    from loguru import Record

_PROGRESS = "Обновляю цены…"
_NAV_ROWS = [[("← К списку", "menu:list"), ("Главное меню", "menu:start")]]
_OK = RefreshReport(total=1, updated=1, failures={})
_SECRET = "dsn=postgres://user:hunter2@db/prices"
_CRASH = RuntimeError(_SECRET)

_Break = Callable[[MagicMock, MagicMock], None]


def _refresh_list_raises(refresher: MagicMock, service: MagicMock) -> None:
    refresher.refresh_list.side_effect = _CRASH


def _list_screen_raises(refresher: MagicMock, service: MagicMock) -> None:
    service.list_page.side_effect = _CRASH


def _refresh_product_raises(refresher: MagicMock, service: MagicMock) -> None:
    refresher.refresh_product.side_effect = _CRASH


def _card_read_raises(refresher: MagicMock, service: MagicMock) -> None:
    service.get_product_card.side_effect = _CRASH


def _gone_screen_raises(refresher: MagicMock, service: MagicMock) -> None:
    refresher.refresh_product.return_value = RefreshGone()
    service.list_page.side_effect = _CRASH


def _vanished_card_screen_raises(refresher: MagicMock, service: MagicMock) -> None:
    service.get_product_card.return_value = None
    service.list_page.side_effect = _CRASH


_LIST_CASES = [
    pytest.param(_refresh_list_raises, id="refresh_list"),
    pytest.param(_list_screen_raises, id="list_screen"),
]
_CARD_CASES = [
    pytest.param(_refresh_product_raises, id="refresh_product"),
    pytest.param(_card_read_raises, id="get_product_card"),
    pytest.param(_gone_screen_raises, id="refresh_gone_render"),
    pytest.param(_vanished_card_screen_raises, id="vanished_card_render"),
]
_ALL_CASES = [
    *[
        pytest.param("list:refresh", c.values[0], id=f"list-{c.id}")
        for c in _LIST_CASES
    ],
    *[
        pytest.param(f"pref:{PRODUCT_ID}", c.values[0], id=f"card-{c.id}")
        for c in _CARD_CASES
    ],
]


@pytest.fixture
def refresher(card_dialog: _Dialog) -> MagicMock:
    mock = cast(MagicMock, card_dialog.dispatcher["price_refresher"])
    mock.reserve.return_value = None
    mock.refresh_list.return_value = _OK
    mock.refresh_product.return_value = _OK
    return mock


@pytest.fixture(autouse=True)
def tracked_list(card_service: MagicMock) -> MagicMock:
    card_service.list_page.return_value = page_of([product()])
    return card_service


def _errors(records: list[Record]) -> list[Record]:
    return [r for r in records if r["level"].name == "ERROR"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("callback_data", "break_it"), _ALL_CASES)
async def test_a_crash_after_the_progress_screen_replaces_it_with_the_generic_error(
    card_dialog: _Dialog,
    refresher: MagicMock,
    card_service: MagicMock,
    callback_data: str,
    break_it: _Break,
) -> None:
    break_it(refresher, card_service)

    await card_dialog.press(callback_data)

    assert [(e.message_id, e.text) for e in card_dialog.edits] == [
        (_CARD_ID, _PROGRESS),
        (_CARD_ID, texts.UNEXPECTED_ERROR_TEXT),
    ]
    assert rows(card_dialog.edits[1]) == _NAV_ROWS


@pytest.mark.asyncio
@pytest.mark.parametrize(("callback_data", "break_it"), _ALL_CASES)
async def test_a_crash_is_not_re_raised_so_the_errors_handler_sends_no_second_copy(
    card_dialog: _Dialog,
    refresher: MagicMock,
    card_service: MagicMock,
    callback_data: str,
    break_it: _Break,
) -> None:
    break_it(refresher, card_service)

    await card_dialog.press(callback_data)

    assert card_dialog.sent == []
    assert [type(r) for r in card_dialog.requests].count(AnswerCallbackQuery) == 1
    assert [type(r) for r in card_dialog.requests].count(EditMessageText) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("callback_data", "break_it"), _ALL_CASES)
async def test_a_crash_is_logged_once_at_error_without_the_screen_text_or_the_secret(
    card_dialog: _Dialog,
    refresher: MagicMock,
    card_service: MagicMock,
    log_records: list[Record],
    callback_data: str,
    break_it: _Break,
) -> None:
    break_it(refresher, card_service)

    await card_dialog.press(callback_data)

    (record,) = _errors(log_records)
    assert record["extra"] == {"user_id": _USER, "error_type": "RuntimeError"}
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
    assert _SECRET not in record["message"]
    assert texts.UNEXPECTED_ERROR_TEXT not in record["message"]
    assert _PROGRESS not in record["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "attribute"),
    [("list:refresh", "refresh_list"), (f"pref:{PRODUCT_ID}", "refresh_product")],
)
async def test_a_refused_error_edit_sends_the_error_as_a_new_message(
    card_dialog: _Dialog, refresher: MagicMock, callback_data: str, attribute: str
) -> None:
    def crash_and_refuse_edits(*_args: int) -> RefreshReport:
        card_dialog.faults.edit_error = TelegramBadRequest(
            method=MagicMock(), message="message can't be edited"
        )
        raise _CRASH

    getattr(refresher, attribute).side_effect = crash_and_refuse_edits

    await card_dialog.press(callback_data)

    assert [s.text for s in card_dialog.sent] == [texts.UNEXPECTED_ERROR_TEXT]
    assert rows(card_dialog.sent[0]) == _NAV_ROWS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "attribute"),
    [("list:refresh", "refresh_list"), (f"pref:{PRODUCT_ID}", "refresh_product")],
)
async def test_a_cancellation_during_the_refresh_is_not_turned_into_an_error_screen(
    card_dialog: _Dialog, refresher: MagicMock, callback_data: str, attribute: str
) -> None:
    getattr(refresher, attribute).side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await card_dialog.press(callback_data)

    assert [e.text for e in card_dialog.edits] == [_PROGRESS]
    assert card_dialog.sent == []
