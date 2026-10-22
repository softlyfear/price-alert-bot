"""Handlers of "🔄 Обновить цены" and "🔄 Обновить цену" (PAB-100 AC1, AC8, AC9).

The boundaries replaced here are ``TrackingService`` (the ``card_service``
double of the conftest) and ``PriceRefresher`` (a ``MagicMock`` put into the
dispatcher data by the ``real_dispatcher`` fixture, as ``app/main.py`` does).
The Bot API is the fake session, so the order and content of the requests are
what is asserted. The clock of the screens is frozen to 2026-10-21 12:00 UTC by
the autouse fixture of the conftest; ``DB_FETCHED_AT`` is 11:05 UTC the same
day, hence "14:05" in Moscow.
"""

from __future__ import annotations

import asyncio
from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.models.enums import Marketplace
from app.services.price_service import RefreshFailure
from app.services.price_service import RefreshGone
from app.services.price_service import RefreshLimiter
from app.services.price_service import RefreshReport
from app.services.price_service import RefreshThrottled
from app.services.tracking import ProductPage
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.card_dialog_support import CARD_PAGE1_NAV
from tests.unit.handlers.card_dialog_support import CARD_TEXT
from tests.unit.handlers.card_dialog_support import NB
from tests.unit.handlers.card_dialog_support import PRODUCT_ID
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import page_of
from tests.unit.handlers.card_dialog_support import product
from tests.unit.handlers.card_dialog_support import rows

_MAX_ID = 2_147_483_647
_LIST_HEAD = (
    "Вы следите за товарами: 1.\nЦены на 14:05\nЦена без учёта персональных скидок."
)
_LIST_ROWS = [
    [(f"1{NB}755{NB}₽ · Из базы <b>1", "pcard:5")],
    [("🔄 Обновить цены", "lref:1")],
    [("➕ Добавить товар", "menu:add"), ("Главное меню", "menu:start")],
]
_CARD_ROWS = [
    [(f"✏️ 1{NB}500{NB}₽", "aedit:11:5"), ("🗑 Убрать", "adel:11:5")],
    [("🔄 Обновить цену", "pref:5")],
    [("➕ Добавить порог", "aadd:5")],
    [("Перестать следить", "pdel:5")],
    CARD_PAGE1_NAV,
]
_GONE_LINE = "Этот товар больше не отслеживается."
_THROTTLED_POPUP = "Обновлять цены можно раз в минуту. Попробуйте через 42 с."
_OK = RefreshReport(total=1, updated=1, failures={})


@pytest.fixture
def refresher(card_dialog: _Dialog) -> MagicMock:
    """The ``PriceRefresher`` double of the dispatcher data, with happy defaults."""
    mock = cast(MagicMock, card_dialog.dispatcher["price_refresher"])
    mock.reserve.return_value = None
    mock.refresh_list.return_value = _OK
    mock.refresh_product.return_value = _OK
    return mock


@pytest.fixture(autouse=True)
def tracked_list(card_service: MagicMock) -> MagicMock:
    """The user tracks one product; ``card_service`` is reused by every test."""
    card_service.list_page.return_value = page_of([product()])
    return card_service


def _texts_and_markups(
    dialog: _Dialog,
) -> list[tuple[str, InlineKeyboardMarkup]]:
    shown: list[tuple[str, InlineKeyboardMarkup]] = []
    for request in dialog.requests:
        if isinstance(request, (SendMessage, EditMessageText)):
            assert isinstance(request.reply_markup, InlineKeyboardMarkup)
            shown.append((request.text, request.reply_markup))
    return shown


def _request_types(dialog: _Dialog) -> list[type]:
    return [type(r) for r in dialog.requests]


def _popups(dialog: _Dialog) -> list[tuple[str | None, bool | None]]:
    return [(a.text, a.show_alert) for a in dialog.callback_answers]


# --- the list button: the happy path ----------------------------------------------


@pytest.mark.asyncio
async def test_list_refresh_edits_in_place_to_progress_then_to_the_summary_screen(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press("list:refresh")

    assert card_dialog.sent == []
    assert [(e.message_id, e.text) for e in card_dialog.edits] == [
        (_CARD_ID, "Обновляю цены…"),
        (_CARD_ID, f"Цены обновлены.\n\n{_LIST_HEAD}"),
    ]
    assert rows(card_dialog.edits[1]) == _LIST_ROWS


@pytest.mark.asyncio
async def test_list_refresh_progress_screen_has_no_buttons_and_no_markup_in_text(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press("list:refresh")

    progress = card_dialog.edits[0]
    assert isinstance(progress.reply_markup, InlineKeyboardMarkup)
    assert progress.reply_markup.inline_keyboard == []
    assert progress.parse_mode is None


@pytest.mark.asyncio
async def test_list_refresh_answers_the_callback_once_without_a_popup(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press("list:refresh")

    assert _popups(card_dialog) == [(None, None)]


@pytest.mark.asyncio
async def test_list_refresh_sends_progress_before_the_refresh_and_the_result_after(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    seen: list[list[type]] = []

    async def refresh(_tg_user_id: int) -> RefreshReport:
        seen.append(_request_types(card_dialog))
        return _OK

    refresher.refresh_list.side_effect = refresh

    await card_dialog.press("list:refresh")

    # at the moment the work runs, the progress edit and the answer are done
    assert seen == [[EditMessageText, AnswerCallbackQuery]]
    assert _request_types(card_dialog) == [
        EditMessageText,
        AnswerCallbackQuery,
        EditMessageText,
    ]


@pytest.mark.asyncio
async def test_list_refresh_spends_the_budget_and_works_for_the_presser(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    await card_dialog.press("list:refresh")

    refresher.reserve.assert_called_once_with(_USER)
    refresher.refresh_list.assert_awaited_once_with(_USER)
    card_service.list_page.assert_awaited_once_with(_USER, 1)


@pytest.mark.asyncio
async def test_list_refresh_redraws_the_list_from_the_database_after_the_refresh(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    order: list[str] = []

    def refresh(_tg_user_id: int) -> RefreshReport:
        order.append("refresh")
        return _OK

    refresher.refresh_list.side_effect = refresh

    async def list_page(_u: int, _page: int) -> ProductPage:
        order.append("list")
        return page_of([product()])

    card_service.list_page.side_effect = list_page

    await card_dialog.press("list:refresh")

    assert order == ["refresh", "list"]


# --- the list button: summaries ---------------------------------------------------


@pytest.mark.asyncio
async def test_list_refresh_partial_result_names_each_non_empty_category(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    refresher.refresh_list.return_value = RefreshReport(
        5, 2, {RefreshFailure.blocked: 2, RefreshFailure.timed_out: 1}
    )

    await card_dialog.press("list:refresh")

    assert card_dialog.edits[1].text == (
        "Обновлено 2 из 5.\n"
        "Маркетплейс заблокировал запрос: 2\n"
        "Не успели: 1\n"
        f"\n{_LIST_HEAD}"
    )


@pytest.mark.asyncio
async def test_list_refresh_of_an_empty_list_shows_the_plain_empty_screen(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    refresher.refresh_list.return_value = RefreshReport(0, 0, {})
    card_service.list_page.return_value = page_of([])

    await card_dialog.press("list:refresh")

    assert card_dialog.edits[1].text == texts.LIST_EMPTY_TEXT
    assert rows(card_dialog.edits[1]) == [
        [("➕ Добавить товар", "menu:add"), ("Главное меню", "menu:start")]
    ]


# --- the list button: refusals and edge cases -------------------------------------


@pytest.mark.asyncio
async def test_list_refresh_over_budget_is_a_popup_with_the_seconds_and_nothing_else(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    refresher.reserve.return_value = RefreshThrottled(42)

    await card_dialog.press("list:refresh")

    assert _request_types(card_dialog) == [AnswerCallbackQuery]
    assert _popups(card_dialog) == [(_THROTTLED_POPUP, True)]
    refresher.refresh_list.assert_not_called()
    card_service.list_page.assert_not_called()


@pytest.mark.asyncio
async def test_list_refresh_without_a_message_is_a_stale_popup_and_spends_no_budget(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press("list:refresh", with_message=False)

    assert _request_types(card_dialog) == [AnswerCallbackQuery]
    assert _popups(card_dialog) == [(texts.LIST_STALE_BUTTON_ALERT_TEXT, True)]
    refresher.reserve.assert_not_called()
    refresher.refresh_list.assert_not_called()


@pytest.mark.asyncio
async def test_list_refresh_on_an_inaccessible_message_works_through_new_messages(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press("list:refresh", accessible=False)

    assert [s.text for s in card_dialog.sent] == ["Обновляю цены…"]
    # the result edits the message that was sent for the progress, not the old one
    assert [(e.message_id, e.text) for e in card_dialog.edits] == [
        (1_000, f"Цены обновлены.\n\n{_LIST_HEAD}")
    ]
    assert _popups(card_dialog) == [(None, None)]


@pytest.mark.asyncio
async def test_list_refresh_whose_result_edit_is_refused_sends_the_result_as_new(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    def refuse_edits_from_now_on(_tg_user_id: int) -> RefreshReport:
        card_dialog.faults.edit_error = TelegramBadRequest(
            method=MagicMock(), message="message can't be edited"
        )
        return _OK

    refresher.refresh_list.side_effect = refuse_edits_from_now_on

    await card_dialog.press("list:refresh")

    assert [e.text for e in card_dialog.edits] == [
        "Обновляю цены…",
        f"Цены обновлены.\n\n{_LIST_HEAD}",  # the refused attempt
    ]
    assert [s.text for s in card_dialog.sent] == [f"Цены обновлены.\n\n{_LIST_HEAD}"]
    assert rows(card_dialog.sent[0]) == _LIST_ROWS
    assert len(card_dialog.callback_answers) == 1


@pytest.mark.asyncio
async def test_list_refresh_network_error_on_the_result_edit_reaches_the_errors_handler(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    def fail_edits_from_now_on(_tg_user_id: int) -> RefreshReport:
        card_dialog.faults.edit_error = TelegramNetworkError(
            method=MagicMock(), message="down"
        )
        return _OK

    refresher.refresh_list.side_effect = fail_edits_from_now_on

    await card_dialog.press("list:refresh")

    assert [s.text for s in card_dialog.sent] == [texts.UNEXPECTED_ERROR_TEXT]


@pytest.mark.asyncio
async def test_two_simultaneous_presses_run_one_refresh_and_show_one_refusal(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    refresher.reserve.side_effect = RefreshLimiter(60.0, lambda: 1_000.0).reserve

    await asyncio.gather(
        card_dialog.press("list:refresh"), card_dialog.press("list:refresh")
    )

    assert refresher.refresh_list.await_count == 1
    assert sorted(_popups(card_dialog), key=lambda p: p[1] is True) == [
        (None, None),
        (texts.refresh_throttled_text(60), True),
    ]
    assert [e.text for e in card_dialog.edits].count("Обновляю цены…") == 1


@pytest.mark.asyncio
async def test_a_refresh_from_the_list_spends_the_budget_of_the_card_button_too(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    refresher.reserve.side_effect = RefreshLimiter(60.0, lambda: 1_000.0).reserve

    await card_dialog.press("list:refresh")
    await card_dialog.press(f"pref:{PRODUCT_ID}")

    refresher.refresh_list.assert_awaited_once()
    refresher.refresh_product.assert_not_called()
    assert _popups(card_dialog)[-1] == (texts.refresh_throttled_text(60), True)


# --- the card button --------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_refresh_edits_in_place_to_progress_then_to_the_card_with_a_summary(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert card_dialog.sent == []
    assert [(e.message_id, e.text) for e in card_dialog.edits] == [
        (_CARD_ID, "Обновляю цены…"),
        (_CARD_ID, f"Цена обновлена.\n\n{CARD_TEXT}"),
    ]
    assert rows(card_dialog.edits[1]) == _CARD_ROWS
    assert _popups(card_dialog) == [(None, None)]


@pytest.mark.asyncio
async def test_card_refresh_scopes_both_the_refresh_and_the_card_read_to_the_presser(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    await card_dialog.press(f"pref:{PRODUCT_ID}")

    refresher.reserve.assert_called_once_with(_USER)
    refresher.refresh_product.assert_awaited_once_with(_USER, PRODUCT_ID)
    card_service.get_product_card.assert_awaited_once_with(_USER, PRODUCT_ID)


@pytest.mark.asyncio
async def test_card_refresh_reads_the_card_after_the_refresh(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    order: list[str] = []

    def refresh(_u: int, _p: int) -> RefreshReport:
        order.append("refresh")
        return _OK

    refresher.refresh_product.side_effect = refresh

    async def get_card(_u: int, _p: int) -> Any:
        order.append("card")
        return card()

    card_service.get_product_card.side_effect = get_card

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert order == ["refresh", "card"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "sentence"),
    [
        (RefreshFailure.transport_error, "Маркетплейс не ответил."),
        (RefreshFailure.out_of_stock, "Товара сейчас нет в наличии."),
        (RefreshFailure.not_found, "Товар на маркетплейсе не нашёлся."),
        (RefreshFailure.bad_payload, "Маркетплейс ответил в неожиданном виде."),
        (RefreshFailure.timed_out, "Не успели получить ответ вовремя."),
        (RefreshFailure.error, "Не удалось сохранить новую цену."),
    ],
)
async def test_card_refresh_failure_gives_one_reason_and_keeps_the_old_price(
    card_dialog: _Dialog,
    refresher: MagicMock,
    reason: RefreshFailure,
    sentence: str,
) -> None:
    refresher.refresh_product.return_value = RefreshReport(1, 0, {reason: 1})

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert card_dialog.edits[1].text == (
        f"{sentence} Показана прежняя цена.\n\n{CARD_TEXT}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marketplace", "name"),
    [(Marketplace.ozon, "Ozon"), (Marketplace.wb, "Wildberries")],
)
async def test_card_refresh_block_names_the_marketplace_of_the_product(
    card_dialog: _Dialog,
    refresher: MagicMock,
    card_service: MagicMock,
    marketplace: Marketplace,
    name: str,
) -> None:
    blocked_card = card()
    blocked_card.product.marketplace = marketplace
    card_service.get_product_card.return_value = blocked_card
    refresher.refresh_product.return_value = RefreshReport(
        1, 0, {RefreshFailure.blocked: 1}
    )

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert card_dialog.edits[1].text.startswith(
        f"{name} заблокировал запрос: он защищается от автоматических обращений. "
        "Показана прежняя цена.\n\n"
    )
    assert f"Маркетплейс: {name}\n" in card_dialog.edits[1].text


@pytest.mark.asyncio
async def test_card_refresh_of_a_gone_product_shows_the_list_and_no_card_data(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    refresher.refresh_product.return_value = RefreshGone()

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert card_dialog.edits[1].text == f"{_GONE_LINE}\n\n{_LIST_HEAD}"
    assert rows(card_dialog.edits[1]) == _LIST_ROWS
    assert "Пороги" not in card_dialog.edits[1].text
    assert "Маркетплейс:" not in card_dialog.edits[1].text
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
async def test_card_refresh_of_a_forged_id_leaks_nothing_about_the_foreign_product(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    refresher.refresh_product.return_value = RefreshGone()
    card_service.list_page.return_value = page_of([])

    await card_dialog.press(f"pref:{_MAX_ID}")

    refresher.refresh_product.assert_awaited_once_with(_USER, _MAX_ID)
    assert card_dialog.edits[1].text == f"{_GONE_LINE}\n\n{texts.LIST_EMPTY_TEXT}"
    assert "Маркетплейс" not in card_dialog.edits[1].text
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
async def test_card_refresh_of_a_product_deleted_meanwhile_shows_the_gone_screen(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    card_service.get_product_card.return_value = None

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert card_dialog.edits[1].text == f"{_GONE_LINE}\n\n{_LIST_HEAD}"
    assert rows(card_dialog.edits[1]) == _LIST_ROWS


@pytest.mark.asyncio
async def test_card_refresh_over_budget_is_a_popup_and_changes_nothing(
    card_dialog: _Dialog, refresher: MagicMock, card_service: MagicMock
) -> None:
    refresher.reserve.return_value = RefreshThrottled(42)

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert _request_types(card_dialog) == [AnswerCallbackQuery]
    assert _popups(card_dialog) == [(_THROTTLED_POPUP, True)]
    refresher.refresh_product.assert_not_called()
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
async def test_card_refresh_without_a_message_is_a_stale_popup_and_spends_no_budget(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press(f"pref:{PRODUCT_ID}", with_message=False)

    assert _popups(card_dialog) == [(texts.LIST_STALE_BUTTON_ALERT_TEXT, True)]
    assert _request_types(card_dialog) == [AnswerCallbackQuery]
    refresher.reserve.assert_not_called()
    refresher.refresh_product.assert_not_called()


@pytest.mark.asyncio
async def test_card_refresh_whose_result_edit_is_refused_sends_the_card_as_new(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    def refuse_edits_from_now_on(_u: int, _p: int) -> RefreshReport:
        card_dialog.faults.edit_error = TelegramBadRequest(
            method=MagicMock(), message="message can't be edited"
        )
        return _OK

    refresher.refresh_product.side_effect = refuse_edits_from_now_on

    await card_dialog.press(f"pref:{PRODUCT_ID}")

    assert [s.text for s in card_dialog.sent] == [f"Цена обновлена.\n\n{CARD_TEXT}"]
    assert rows(card_dialog.sent[0]) == _CARD_ROWS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix", ["0", "-1", "abc", "", "1:2", "1.5", str(_MAX_ID + 1)]
)
async def test_forged_refresh_payload_gets_the_stale_popup_and_reaches_nothing(
    card_dialog: _Dialog,
    refresher: MagicMock,
    card_service: MagicMock,
    suffix: str,
) -> None:
    await card_dialog.press(f"pref:{suffix}")

    assert _request_types(card_dialog) == [AnswerCallbackQuery]
    assert _popups(card_dialog) == [(texts.LIST_STALE_BUTTON_ALERT_TEXT, True)]
    refresher.reserve.assert_not_called()
    refresher.refresh_product.assert_not_called()
    card_service.get_product_card.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_payload_without_any_id_part_gets_the_stale_popup(
    card_dialog: _Dialog, refresher: MagicMock
) -> None:
    await card_dialog.press("pref")

    assert _popups(card_dialog) == [(texts.LIST_STALE_BUTTON_ALERT_TEXT, True)]
    refresher.reserve.assert_not_called()
