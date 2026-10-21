"""Tests for changing a threshold from the product card (PAB-095).

Everything goes through ``Dispatcher.feed_update`` on the real
``create_dispatcher()`` output with a fake ``Bot`` session, so the router
order, the state filters and the ``CallbackData`` filters are exercised too.
The replaced boundary is ``TrackingService``: a ``MagicMock(spec=...)`` returns
what a scenario programs and records every call. The dialog doubles come from
``add_dialog_support``. Business rules (direction, duplicates, the SAVEPOINT)
live in the service and are covered by the service tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message

import app.handlers.add_product as add_product_module
import app.handlers.list_products as list_products_module
from app.bot import texts
from app.bot.states import AddProduct
from app.bot.states import EditAlert
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.services.tracking import AlertChange
from app.services.tracking import ProductCard
from app.services.tracking import TrackingService
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.conftest import make_message_update
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _NAME
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder

_NB = "\u00a0"
_MENU = ["menu:add", "menu:list", "menu:help"]
_CARD_NAV = [("← К списку", "menu:list"), ("Главное меню", "menu:start")]
_CANCEL_ROWS = [[("Отмена", "edit:cancel")]]
_LEGEND = "↓ — сообщу, когда цена снизится до порога, ↑ — когда вырастет до него."
_CARD_HEAD = (
    f"{_NAME}\n"
    "Маркетплейс: Wildberries\n"
    "https://www.wildberries.ru/catalog/12345678/detail.aspx\n"
    f"Текущая цена: 1{_NB}990{_NB}₽\n"
    "Цена без учёта персональных скидок.\n"
    "\n"
    "Пороги:\n"
)
_EDIT_STATE = EditAlert.waiting_target_price.state
_EDIT_DATA: dict[str, Any] = {"alert_id": 11, "product_id": 5}


def _product(product_id: int = 5) -> Product:
    return Product(
        id=product_id,
        user_id=1,
        marketplace=Marketplace.wb,
        article=12345678,
        product_name=_NAME,
        current_price=199_000,
    )


def _alert(alert_id: int, direction: AlertDirection, target: int) -> Alert:
    return Alert(
        id=alert_id, user_id=1, product_id=5, target_price=target, direction=direction
    )


def _card(*alerts: Alert) -> ProductCard:
    return ProductCard(product=_product(), alerts=tuple(alerts))


def _rows(request: SendMessage | EditMessageText) -> list[list[tuple[str, str | None]]]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(spec=TrackingService)
    mock.get_product_card.return_value = None
    mock.change_alert_target.return_value = AlertChange.changed
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: mock)
    return mock


@pytest.fixture
def dialog(real_dispatcher: Dispatcher, service: MagicMock) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


async def _at_edit(dialog: _Dialog, data: dict[str, Any] | None = None) -> None:
    await dialog.set_state(_EDIT_STATE, dict(_EDIT_DATA if data is None else data))


# --- pressing "Изменить порог" -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "arrow"), [(AlertDirection.below, "↓"), (AlertDirection.above, "↑")]
)
async def test_press_edit_replaces_the_card_with_a_prompt_and_waits_for_the_amount(
    dialog: _Dialog, service: MagicMock, direction: AlertDirection, arrow: str
) -> None:
    service.get_product_card.return_value = _card(_alert(11, direction, 150_000))

    await dialog.press("aedit:11:5")

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    service.change_alert_target.assert_not_called()
    assert dialog.sent == []
    assert len(dialog.edits) == 1
    edit = dialog.edits[0]
    assert (edit.chat_id, edit.message_id) == (_CHAT, _CARD_ID)
    assert edit.parse_mode is None
    assert edit.text.startswith(
        f"{_NAME}\nТекущая цена: 1{_NB}990{_NB}₽\nЦена без учёта персональных скидок.\n"
        f"Изменяемый порог: {arrow} 1{_NB}500{_NB}₽\n"
    )
    assert _rows(edit) == _CANCEL_ROWS
    assert await dialog.state() == _EDIT_STATE
    assert await dialog.data() == {"alert_id": 11, "product_id": 5}
    assert len(dialog.callback_answers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        AddProduct.waiting_link.state,
        AddProduct.waiting_target_price.state,
        AddProduct.confirming.state,
    ],
)
async def test_press_edit_replaces_a_running_add_dialog_and_drops_its_draft(
    dialog: _Dialog, service: MagicMock, state: str
) -> None:
    service.get_product_card.return_value = _card(
        _alert(11, AlertDirection.below, 150_000)
    )
    await dialog.set_state(state, {"marketplace": "wb", "article": 1, "draft": "x"})

    await dialog.press("aedit:11:5")

    assert await dialog.state() == _EDIT_STATE
    assert await dialog.data() == {"alert_id": 11, "product_id": 5}


@pytest.mark.asyncio
async def test_press_edit_for_a_removed_alert_redraws_the_card_and_sets_no_state(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.get_product_card.return_value = _card(
        _alert(12, AlertDirection.above, 250_000)
    )

    await dialog.press("aedit:11:5")

    service.change_alert_target.assert_not_called()
    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.ALERT_ALREADY_REMOVED_TEXT, None)
    ]
    assert [(e.text, e.parse_mode) for e in dialog.edits] == [
        (f"{_CARD_HEAD}• ↑ 2{_NB}500{_NB}₽\n{_LEGEND}", None)
    ]
    assert _rows(dialog.edits[0]) == [
        [(f"Изменить порог: 2{_NB}500{_NB}₽", "aedit:12:5")],
        [(f"Убрать порог: 2{_NB}500{_NB}₽", "adel:12:5")],
        [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:5")],
        _CARD_NAV,
    ]
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_press_edit_for_a_missing_or_foreign_product_says_it_is_gone(
    dialog: _Dialog, service: MagicMock
) -> None:
    """The card read is scoped to the pressing user: a forged id yields no card."""
    await dialog.press("aedit:11:777")

    service.get_product_card.assert_awaited_once_with(_USER, 777)
    service.change_alert_target.assert_not_called()
    assert [(e.text, e.parse_mode) for e in dialog.edits] == [
        (texts.PRODUCT_ALREADY_GONE_TEXT, None)
    ]
    assert _rows(dialog.edits[0]) == [_CARD_NAV]
    assert await dialog.state() is None
    assert len(dialog.callback_answers) == 1


@pytest.mark.asyncio
async def test_press_edit_without_a_message_pops_up_stale_and_reads_nothing(
    dialog: _Dialog, service: MagicMock
) -> None:
    await dialog.press("aedit:11:5", with_message=False)

    service.get_product_card.assert_not_called()
    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert dialog.sent == []
    assert dialog.edits == []
    assert await dialog.state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    ["aedit", "aedit:11", "aedit:0:5", "aedit:11:0", "aedit:x:y", "aedit:1:2:3"],
)
async def test_broken_edit_payload_gets_the_stale_reply_and_never_reaches_the_service(
    dialog: _Dialog, service: MagicMock, payload: str
) -> None:
    await dialog.press(payload)

    service.get_product_card.assert_not_called()
    service.change_alert_target.assert_not_called()
    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert await dialog.state() is None


# --- typing the amount ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "phrase"),
    [
        (AlertDirection.below, f"цена станет ниже 1{_NB}500{_NB}₽"),
        (AlertDirection.above, f"цена станет выше 1{_NB}500{_NB}₽"),
    ],
)
async def test_changed_sends_one_new_message_with_the_outcome_and_the_redrawn_card(
    dialog: _Dialog, service: MagicMock, direction: AlertDirection, phrase: str
) -> None:
    service.change_alert_target.return_value = AlertChange.changed
    service.get_product_card.return_value = _card(_alert(11, direction, 150_000))
    await _at_edit(dialog)

    await dialog.say("1500")

    service.change_alert_target.assert_awaited_once_with(_USER, 11, 150_000)
    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert dialog.edits == []
    assert len(dialog.sent) == 1
    sent = dialog.sent[0]
    arrow = "↓" if direction is AlertDirection.below else "↑"
    assert sent.text == (
        f"Порог изменён. Сообщу, когда {phrase}.\n\n"
        f"{_CARD_HEAD}• {arrow} 1{_NB}500{_NB}₽\n{_LEGEND}"
    )
    assert sent.parse_mode is None
    assert _rows(sent) == [
        [(f"Изменить порог: 1{_NB}500{_NB}₽", "aedit:11:5")],
        [(f"Убрать порог: 1{_NB}500{_NB}₽", "adel:11:5")],
        [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:5")],
        _CARD_NAV,
    ]
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_changed_names_no_direction_when_the_alert_left_the_card_meanwhile(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.change_alert_target.return_value = AlertChange.changed
    service.get_product_card.return_value = _card(
        _alert(12, AlertDirection.above, 250_000)
    )
    await _at_edit(dialog)

    await dialog.say("1500")

    assert len(dialog.sent) == 1
    assert dialog.sent[0].text.startswith("Порог изменён.\n\n" + _CARD_HEAD)
    assert "Сообщу" not in dialog.sent[0].text
    assert await dialog.state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "summary"),
    [
        (AlertChange.unchanged, "Порог не изменился."),
        (AlertChange.not_found, "Порог уже удалён"),
    ],
)
async def test_unchanged_and_not_found_end_with_their_line_and_the_card(
    dialog: _Dialog, service: MagicMock, outcome: AlertChange, summary: str
) -> None:
    service.change_alert_target.return_value = outcome
    service.get_product_card.return_value = _card(
        _alert(11, AlertDirection.below, 150_000)
    )
    await _at_edit(dialog)

    await dialog.say("1500")

    assert [m.text for m in dialog.sent] == [
        f"{summary}\n\n{_CARD_HEAD}• ↓ 1{_NB}500{_NB}₽\n{_LEGEND}"
    ]
    assert _rows(dialog.sent[0])[-1] == _CARD_NAV
    assert dialog.edits == []
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_product_gone_after_the_change_ends_with_the_gone_text_and_main_menu(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.get_product_card.return_value = None
    await _at_edit(dialog)

    await dialog.say("1500")

    assert [m.text for m in dialog.sent] == [texts.PRODUCT_ALREADY_GONE_TEXT]
    assert [b for _, b in _rows(dialog.sent[0])[0] + _rows(dialog.sent[0])[1]] == _MENU
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_amount_is_converted_to_kopecks_and_the_user_comes_from_the_sender(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.get_product_card.return_value = _card(
        _alert(11, AlertDirection.below, 150_050)
    )
    await _at_edit(dialog, {"alert_id": 31, "product_id": 5})

    await dialog.say("1 500,50")

    service.change_alert_target.assert_awaited_once_with(_USER, 31, 150_050)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["hello", "0", "-5", "1,234", "   ", "9" * 10_000])
async def test_invalid_amount_keeps_the_step_and_never_calls_the_service(
    dialog: _Dialog, service: MagicMock, bad: str
) -> None:
    await _at_edit(dialog)

    await dialog.say(bad)

    service.change_alert_target.assert_not_called()
    assert [m.text for m in dialog.sent] == [texts.ADD_PRICE_INVALID_TEXT]
    assert _rows(dialog.sent[0]) == _CANCEL_ROWS
    assert await dialog.state() == _EDIT_STATE
    assert await dialog.data() == _EDIT_DATA


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TargetEqualsCurrentPriceError(), texts.ADD_TARGET_EQUALS_TEXT),
        (DuplicateAlertError(), texts.EDIT_DUPLICATE_TEXT),
    ],
    ids=["equals-current-price", "duplicate"],
)
async def test_domain_refusal_keeps_the_step_and_asks_for_another_amount(
    dialog: _Dialog, service: MagicMock, error: Exception, expected: str
) -> None:
    service.change_alert_target.side_effect = error
    await _at_edit(dialog)

    await dialog.say("1990")

    service.change_alert_target.assert_awaited_once_with(_USER, 11, 199_000)
    service.get_product_card.assert_not_called()
    assert [m.text for m in dialog.sent] == [expected]
    assert _rows(dialog.sent[0]) == _CANCEL_ROWS
    assert await dialog.state() == _EDIT_STATE
    assert await dialog.data() == _EDIT_DATA


@pytest.mark.asyncio
async def test_non_text_message_keeps_the_step_and_asks_for_text(
    dialog: _Dialog, service: MagicMock
) -> None:
    await _at_edit(dialog)

    await dialog.feed(
        make_message_update(photo=True, update_id=50, chat_id=_CHAT, user_id=_USER)
    )

    service.change_alert_target.assert_not_called()
    assert [m.text for m in dialog.sent] == [texts.EDIT_NOT_TEXT_TEXT]
    assert _rows(dialog.sent[0]) == _CANCEL_ROWS
    assert await dialog.state() == _EDIT_STATE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [{}, {"alert_id": 11}, {"product_id": 5}, {"alert_id": "11", "product_id": 5}],
    ids=["empty", "no-product", "no-alert", "alert-not-int"],
)
async def test_corrupted_state_data_reports_stale_clears_and_never_calls_the_service(
    dialog: _Dialog, service: MagicMock, data: dict[str, Any]
) -> None:
    await _at_edit(dialog, data)

    await dialog.say("1500")

    service.change_alert_target.assert_not_called()
    assert [m.text for m in dialog.sent] == [texts.EDIT_STALE_TEXT]
    assert [b for row in _rows(dialog.sent[0]) for _, b in row] == _MENU
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_free_text_in_the_edit_state_is_not_answered_by_the_fallback(
    dialog: _Dialog, service: MagicMock
) -> None:
    await _at_edit(dialog)

    await dialog.say("не знаю")

    assert texts.FALLBACK_TEXT not in [m.text for m in dialog.sent]
    assert len(dialog.sent) == 1


@pytest.mark.asyncio
async def test_add_command_in_the_edit_state_restarts_the_add_dialog(
    dialog: _Dialog, service: MagicMock
) -> None:
    await _at_edit(dialog)

    await dialog.command("add")

    service.change_alert_target.assert_not_called()
    assert [m.text for m in dialog.sent] == [texts.ADD_PROMPT_LINK_TEXT]
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_service_failure_reaches_the_errors_handler_and_keeps_no_success_text(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.change_alert_target.side_effect = RuntimeError("boom")
    await _at_edit(dialog)

    await dialog.say("1500")

    assert [m.text for m in dialog.sent] == [texts.UNEXPECTED_ERROR_TEXT]


@pytest.mark.asyncio
async def test_unknown_change_outcome_is_an_error_not_a_silent_success(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.change_alert_target.return_value = "vanished"
    service.get_product_card.return_value = _card(
        _alert(11, AlertDirection.below, 150_000)
    )
    await _at_edit(dialog)

    await dialog.say("1500")

    assert [m.text for m in dialog.sent] == [texts.UNEXPECTED_ERROR_TEXT]


def _handler(name: str) -> Any:
    router = list_products_module.create_router()
    for observer in router.observers.values():
        for handler in observer.handlers:
            if handler.callback.__name__ == name:
                return handler.callback
    raise AssertionError(name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_sender", "text"),
    [(False, "1500"), (True, None)],
    ids=["no-sender", "no-text"],
)
async def test_price_handler_ignores_a_message_without_sender_or_text(
    has_sender: bool, text: str | None
) -> None:
    """Defensive guard behind the filters: reached only by a direct call."""
    message = MagicMock(spec=Message)
    message.text = text
    message.from_user = MagicMock() if has_sender else None
    message.answer = AsyncMock()
    state = MagicMock()
    tracking = MagicMock()
    tracking.change_alert_target = AsyncMock()

    await _handler("on_edit_price")(message, state, tracking)

    message.answer.assert_not_called()
    tracking.change_alert_target.assert_not_called()
    assert state.mock_calls == []


# --- "Отмена" -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_puts_the_card_back_in_place_and_clears_the_state(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.get_product_card.return_value = _card(
        _alert(11, AlertDirection.below, 150_000)
    )
    await _at_edit(dialog)

    await dialog.press("edit:cancel")

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    service.change_alert_target.assert_not_called()
    assert dialog.sent == []
    assert [(e.message_id, e.text, e.parse_mode) for e in dialog.edits] == [
        (_CARD_ID, f"{_CARD_HEAD}• ↓ 1{_NB}500{_NB}₽\n{_LEGEND}", None)
    ]
    assert _rows(dialog.edits[0])[0] == (
        [(f"Изменить порог: 1{_NB}500{_NB}₽", "aedit:11:5")]
    )
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_stale_cancel_without_state_data_says_cancelled_with_the_main_menu(
    dialog: _Dialog, service: MagicMock
) -> None:
    await dialog.press("edit:cancel")

    service.get_product_card.assert_not_called()
    assert [e.text for e in dialog.edits] == [texts.CANCEL_TEXT]
    assert [b for row in _rows(dialog.edits[0]) for _, b in row] == _MENU
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_cancel_with_a_vanished_product_says_cancelled_with_the_main_menu(
    dialog: _Dialog, service: MagicMock
) -> None:
    service.get_product_card.return_value = None
    await _at_edit(dialog)

    await dialog.press("edit:cancel")

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert [e.text for e in dialog.edits] == [texts.CANCEL_TEXT]
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_cancel_with_a_non_integer_product_id_never_reads_the_card(
    dialog: _Dialog, service: MagicMock
) -> None:
    await _at_edit(dialog, {"alert_id": 11, "product_id": "5"})

    await dialog.press("edit:cancel")

    service.get_product_card.assert_not_called()
    assert [e.text for e in dialog.edits] == [texts.CANCEL_TEXT]
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_cancel_without_a_message_pops_up_cancelled_and_still_clears_the_state(
    dialog: _Dialog, service: MagicMock
) -> None:
    # Without a message Telegram gives no chat, so aiogram keys the state by user.
    key = StorageKey(bot_id=dialog.bot.id, chat_id=_USER, user_id=_USER)
    storage = dialog.dispatcher.storage
    await storage.set_state(key, _EDIT_STATE)
    await storage.set_data(key, dict(_EDIT_DATA))

    await dialog.press("edit:cancel", with_message=False)

    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.CANCEL_TEXT, True)
    ]
    assert dialog.sent == []
    assert dialog.edits == []
    assert await storage.get_state(key) is None


@pytest.mark.asyncio
async def test_cancel_command_clears_the_edit_state_and_free_text_gets_the_fallback(
    dialog: _Dialog, service: MagicMock
) -> None:
    await _at_edit(dialog)

    await dialog.command("cancel")

    assert [m.text for m in dialog.sent] == [texts.CANCEL_TEXT]
    assert await dialog.state() is None

    await dialog.say("привет")

    assert [m.text for m in dialog.sent][-1] == texts.FALLBACK_TEXT
    service.change_alert_target.assert_not_called()
