"""Tests for the /add dialog, app.handlers.add_product (PAB-068 AC6).

Every scenario goes through ``Dispatcher.feed_update`` on the real
``create_dispatcher()`` output with a fake ``Bot`` session, so filter and
middleware wiring is exercised too. The boundary replaced is
``TrackingService`` (the handler's only collaborator): the stub records every
call and raises or returns what a scenario programs. Business rules live in
the service and are covered by ``tests/unit/services/test_tracking_service.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.setup import create_dispatcher
from app.bot.states import AddProduct
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_command_update
from tests.unit.conftest import make_message_update
from tests.unit.conftest import send_message_responder
from tests.unit.conftest import sent_message_id
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _CONFIRMING_DATA
from tests.unit.handlers.add_dialog_support import _LINK_A
from tests.unit.handlers.add_dialog_support import _MAIN_MENU
from tests.unit.handlers.add_dialog_support import _MSG_A
from tests.unit.handlers.add_dialog_support import _NAME
from tests.unit.handlers.add_dialog_support import _NAME_A
from tests.unit.handlers.add_dialog_support import _NAME_B
from tests.unit.handlers.add_dialog_support import _OUTCOMES
from tests.unit.handlers.add_dialog_support import _PRICE
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _WAIT
from tests.unit.handlers.add_dialog_support import _assert_only_dropped_edit
from tests.unit.handlers.add_dialog_support import _assert_shown_as
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Call
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _GatedTracking
from tests.unit.handlers.add_dialog_support import _keyboard_callbacks
from tests.unit.handlers.add_dialog_support import _Outcome
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.add_dialog_support import _shown_texts
from tests.unit.handlers.add_dialog_support import _TrackingStub
from tests.unit.wb_access_stub import stub_client_factory

if TYPE_CHECKING:
    from loguru import Record

_NBSP = " "
_WB_LINK = "https://www.wildberries.ru/catalog/12345678/detail.aspx"


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _TrackingStub:
    tracking = _TrackingStub()

    def build(**kwargs: Any) -> _TrackingStub:
        tracking.init_kwargs.append(kwargs)
        return tracking

    monkeypatch.setattr(add_product_module, "TrackingService", build)
    return tracking


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


async def _at_confirming(dialog: _Dialog) -> None:
    await dialog.set_state(AddProduct.confirming.state, dict(_CONFIRMING_DATA))


# --- entry -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_command_enters_waiting_link_with_prompt_and_cancel_button(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")

    assert await dialog.state() == AddProduct.waiting_link.state
    assert len(dialog.sent) == 1
    assert dialog.last_text == texts.ADD_PROMPT_LINK_TEXT
    assert _keyboard_callbacks(dialog.sent[0]) == [CB_ADD_CANCEL]


@pytest.mark.asyncio
async def test_add_command_restarts_a_running_dialog_and_drops_its_draft(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_confirming(dialog)

    await dialog.command("add")

    assert await dialog.state() == AddProduct.waiting_link.state
    assert await dialog.data() == {}


# --- link step -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["hello", "https://evil.com/x", "0", "   "])
async def test_unparseable_link_shows_format_hint_and_keeps_state(
    dialog: _Dialog, stub: _TrackingStub, bad: str
) -> None:
    await dialog.command("add")

    await dialog.say(bad)

    assert dialog.last_text == texts.ADD_LINK_FORMAT_HINT_TEXT
    assert await dialog.state() == AddProduct.waiting_link.state
    assert stub.preview_calls == []


@pytest.mark.asyncio
async def test_unsupported_marketplace_at_link_step_keeps_dialog_with_cancel_keyboard(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_error = MarketplaceNotSupportedError("ozon")
    await dialog.command("add")

    await dialog.say("https://www.ozon.ru/product/slug-1234567/")

    assert stub.preview_calls == [(_USER, Marketplace.ozon, 1234567)]
    assert dialog.outcome.text == texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT
    assert (
        dialog.outcome.text
        != texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.not_found]
    )
    assert await dialog.state() == AddProduct.waiting_link.state
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CANCEL]


def test_unsupported_marketplace_text_is_true_in_both_places_it_is_shown() -> None:
    """Shown with the cancel keyboard (link step) and with no dialog at all
    (confirmation), so it names no marketplace and asks for no action."""
    forbidden = ("Wildberries", "Ozon", "Отмена", "Пришлите")
    text = texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT
    # positive control: the check does catch the text this one replaced
    old = (
        "Этот маркетплейс пока не поддерживается. Пришлите ссылку или артикул "
        "Wildberries либо нажмите «Отмена»."
    )

    assert [f for f in forbidden if f in old] == ["Wildberries", "Отмена", "Пришлите"]
    assert "не поддерживается" in text
    assert [f for f in forbidden if f in text] == []


@pytest.mark.asyncio
async def test_preview_limit_shows_the_limit_and_clears_the_state(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_error = ProductLimitExceededError(37)
    await dialog.command("add")

    await dialog.say(_WB_LINK)

    assert "37" in dialog.outcome.text
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_preview_receives_telegram_user_id_of_the_sender_not_the_chat(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")

    await dialog.say("12345678")

    assert stub.preview_calls == [(_USER, Marketplace.wb, 12345678)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "keyword"),
    [
        (FetchFailureReason.not_found, "не нашёлся"),
        (FetchFailureReason.blocked, "заблокировал"),
        (FetchFailureReason.transport_error, "Не удалось получить"),
        (FetchFailureReason.bad_payload, "неожиданном виде"),
        (FetchFailureReason.out_of_stock, "нет в наличии"),
    ],
)
async def test_each_fetch_failure_has_its_own_text_and_keeps_the_state(
    dialog: _Dialog, stub: _TrackingStub, reason: FetchFailureReason, keyword: str
) -> None:
    stub.preview_result = MarketplaceFetchFailure(
        reason=reason, detail="SECRET-DETAIL-MARKER"
    )
    await dialog.command("add")

    await dialog.say(_WB_LINK)

    assert keyword in dialog.outcome.text
    assert "SECRET-DETAIL-MARKER" not in dialog.outcome.text
    assert await dialog.state() == AddProduct.waiting_link.state
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CANCEL]


def test_fetch_failure_texts_are_five_distinct_strings() -> None:
    assert set(texts.ADD_FETCH_FAILURE_TEXTS) == set(FetchFailureReason)
    assert len(set(texts.ADD_FETCH_FAILURE_TEXTS.values())) == 5


def test_transport_error_text_does_not_promise_a_retry_later() -> None:
    """401/451/400 land in ``transport_error`` and may be permanent."""
    text = texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.transport_error]

    assert "позже" not in text


def test_blocked_text_names_a_marketplace_side_block() -> None:
    text = texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.blocked]

    assert "Маркетплейс заблокировал" in text


@pytest.mark.asyncio
async def test_found_product_card_has_name_price_disclaimer_and_plain_text(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")

    await dialog.say(_WB_LINK, message_id=77)

    card = dialog.outcome
    assert _NAME in card.text
    assert f"1{_NBSP}990{_NBSP}₽" in card.text
    assert PRICE_DISCLAIMER_SHORT in card.text
    assert card.parse_mode is None
    assert _keyboard_callbacks(card) == [CB_ADD_CANCEL]
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert await dialog.data() == {
        "marketplace": "wb",
        "article": 12345678,
        "product_name": _NAME,
        "current_price": _PRICE,
        "link_message_id": 77,  # PAB-091 marker: the id of the link message
    }


@pytest.mark.asyncio
async def test_non_text_on_the_link_step_asks_for_text_and_keeps_the_state(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")

    await dialog.feed(
        make_message_update(photo=True, update_id=50, chat_id=_CHAT, user_id=_USER)
    )

    assert dialog.last_text == texts.ADD_NOT_TEXT_TEXT
    assert await dialog.state() == AddProduct.waiting_link.state
    assert stub.preview_calls == []


@pytest.mark.asyncio
async def test_message_without_sender_on_link_step_is_ignored_silently(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.set_state(AddProduct.waiting_link.state)

    await dialog.feed(
        make_message_update(text=_WB_LINK, update_id=60, chat_id=_CHAT, user_id=None)
    )

    assert dialog.sent == []
    assert stub.preview_calls == []


# --- price step ----------------------------------------------------------------


async def _at_waiting_price(dialog: _Dialog) -> None:
    await dialog.set_state(
        AddProduct.waiting_target_price.state,
        {
            "marketplace": "wb",
            "article": 12345678,
            "product_name": _NAME,
            "current_price": _PRICE,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["0", "-5", "abc", "1e3", "1990,555", "99999999999", "   ", "₽"]
)
async def test_invalid_price_shows_text_and_keeps_state_and_draft(
    dialog: _Dialog, stub: _TrackingStub, bad: str
) -> None:
    await _at_waiting_price(dialog)

    await dialog.say(bad)

    assert dialog.last_text == texts.ADD_PRICE_INVALID_TEXT
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert "target_price" not in await dialog.data()


@pytest.mark.asyncio
async def test_price_in_rubles_is_converted_to_kopecks_at_the_bot_boundary(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)

    await dialog.say("1990,5")

    assert (await dialog.data())["target_price"] == 199_050
    assert await dialog.state() == AddProduct.confirming.state


@pytest.mark.asyncio
async def test_confirmation_message_has_name_prices_disclaimer_and_buttons(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)

    await dialog.say("1500")

    confirmation = dialog.sent[-1]
    assert _NAME in confirmation.text
    assert f"1{_NBSP}990{_NBSP}₽" in confirmation.text
    assert f"1{_NBSP}500{_NBSP}₽" in confirmation.text
    assert PRICE_DISCLAIMER_SHORT in confirmation.text
    assert confirmation.parse_mode is None
    assert _keyboard_callbacks(confirmation) == [CB_ADD_CONFIRM, CB_ADD_CANCEL]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [{}, {"product_name": _NAME}, {"product_name": 5, "current_price": _PRICE}],
    ids=["empty", "no-price", "wrong-type"],
)
async def test_corrupted_draft_on_the_price_step_reports_stale_and_clears(
    dialog: _Dialog, stub: _TrackingStub, broken: dict[str, Any]
) -> None:
    await dialog.set_state(AddProduct.waiting_target_price.state, broken)

    await dialog.say("1500")

    assert dialog.last_text == texts.ADD_STALE_TEXT
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_non_text_on_the_price_step_asks_for_text_and_keeps_the_state(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)

    await dialog.feed(
        make_message_update(photo=True, update_id=70, chat_id=_CHAT, user_id=_USER)
    )

    assert dialog.last_text == texts.ADD_NOT_TEXT_TEXT
    assert await dialog.state() == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
async def test_text_on_the_confirmation_step_points_at_the_buttons(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_confirming(dialog)

    await dialog.say("yes")

    assert dialog.last_text == texts.ADD_CONFIRM_HINT_TEXT
    assert _keyboard_callbacks(dialog.sent[-1]) == [CB_ADD_CONFIRM, CB_ADD_CANCEL]
    assert await dialog.state() == AddProduct.confirming.state
    assert stub.add_calls == []


# --- confirmation ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_adds_tracking_for_the_pressing_user_and_clears_the_state(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_confirming(dialog)

    await dialog.press(CB_ADD_CONFIRM)

    assert stub.add_calls == [(_USER, Marketplace.wb, 12345678, _NAME, _PRICE, 150_000)]
    assert len(dialog.callback_answers) == 1
    assert dialog.last_text.startswith("Отслеживание добавлено")
    assert _NAME in dialog.last_text
    assert f"1{_NBSP}500{_NBSP}₽" in dialog.last_text
    assert PRICE_DISCLAIMER_SHORT in dialog.last_text
    assert dialog.sent[-1].parse_mode is None
    assert await dialog.state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProductLimitExceededError(37), texts.limit_exceeded_text(37)),
        (
            MarketplaceNotSupportedError("ozon"),
            texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT,
        ),
    ],
    ids=["limit", "unsupported"],
)
async def test_confirm_refusals_have_their_own_text_and_clear_the_state(
    dialog: _Dialog, stub: _TrackingStub, error: Exception, expected: str
) -> None:
    stub.add_error = error
    await _at_confirming(dialog)

    await dialog.press(CB_ADD_CONFIRM)

    assert dialog.last_text == expected
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() is None


def test_refusal_texts_differ_from_each_other_and_the_limit_shows_its_number() -> None:
    assert "37" in texts.limit_exceeded_text(37)
    assert texts.limit_exceeded_text(37) != texts.ALERT_DUPLICATE_TEXT
    assert texts.ALERT_DUPLICATE_TEXT != texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, AddProduct.waiting_link.state])
async def test_confirm_outside_the_confirmation_step_is_stale_and_never_calls_service(
    dialog: _Dialog, stub: _TrackingStub, state: str | None
) -> None:
    if state is not None:
        await dialog.set_state(state, dict(_CONFIRMING_DATA))

    await dialog.press(CB_ADD_CONFIRM)

    assert stub.add_calls == []
    assert len(dialog.callback_answers) == 1
    assert dialog.callback_answers[0].text == texts.ADD_STALE_ALERT_TEXT
    assert dialog.callback_answers[0].show_alert is True
    assert dialog.sent == []
    assert await dialog.state() == state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        {"confirm_message_id": _CARD_ID},
        {**_CONFIRMING_DATA, "marketplace": "amazon"},
        {**_CONFIRMING_DATA, "article": "12345678"},
        {**_CONFIRMING_DATA, "product_name": 5},
        {**_CONFIRMING_DATA, "current_price": "1"},
        {**_CONFIRMING_DATA, "target_price": None},
        {k: v for k, v in _CONFIRMING_DATA.items() if k != "target_price"},
    ],
    ids=[
        "empty",
        "bad-marketplace",
        "article-type",
        "name-type",
        "price-type",
        "target-none",
        "target-missing",
    ],
)
async def test_confirm_with_corrupted_draft_reports_stale_and_never_calls_service(
    dialog: _Dialog, stub: _TrackingStub, broken: dict[str, Any]
) -> None:
    await dialog.set_state(AddProduct.confirming.state, broken)

    await dialog.press(CB_ADD_CONFIRM)

    assert stub.add_calls == []
    assert dialog.last_text == texts.ADD_STALE_TEXT
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() is None


def test_callback_data_constants_carry_no_identifiers() -> None:
    assert CB_ADD_CONFIRM == "add:confirm"
    assert CB_ADD_CANCEL == "add:cancel"


# --- cancel --------------------------------------------------------------------


_STEPS = [
    AddProduct.waiting_link.state,
    AddProduct.waiting_target_price.state,
    AddProduct.confirming.state,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _STEPS)
async def test_cancel_command_clears_the_dialog_on_every_step(
    dialog: _Dialog, stub: _TrackingStub, state: str
) -> None:
    await dialog.set_state(state, dict(_CONFIRMING_DATA))

    await dialog.command("cancel")

    assert dialog.last_text == texts.CANCEL_TEXT
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _STEPS)
async def test_cancel_button_clears_the_dialog_on_every_step_and_answers(
    dialog: _Dialog, stub: _TrackingStub, state: str
) -> None:
    await dialog.set_state(state, dict(_CONFIRMING_DATA))

    await dialog.press(CB_ADD_CANCEL)

    assert dialog.last_text == texts.CANCEL_TEXT
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() is None
    assert await dialog.data() == {}
    assert stub.add_calls == []


@pytest.mark.asyncio
async def test_cancel_button_without_a_dialog_is_harmless(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.press(CB_ADD_CANCEL)

    assert len(dialog.callback_answers) == 1
    assert dialog.last_text == texts.CANCEL_TEXT
    assert await dialog.state() is None


_EMPTY_STATE: tuple[str | None, dict[str, Any]] = (None, {})
_FULL_STATE: tuple[str | None, dict[str, Any]] = (
    AddProduct.confirming.state,
    dict(_CONFIRMING_DATA),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("accessible", [True, False], ids=["message", "inaccessible"])
@pytest.mark.parametrize("fsm", [_FULL_STATE, _EMPTY_STATE], ids=["data", "empty"])
async def test_cancel_button_on_a_message_with_a_chat_sends_one_plain_cancel_text(
    dialog: _Dialog,
    stub: _TrackingStub,
    accessible: bool,
    fsm: tuple[str | None, dict[str, Any]],
) -> None:
    await dialog.set_state(*fsm)

    await dialog.press(CB_ADD_CANCEL, accessible=accessible)

    assert [(m.chat_id, m.text, m.parse_mode) for m in dialog.sent] == [
        (_CHAT, texts.CANCEL_TEXT, None)
    ]
    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [(None, None)]
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fsm", [_FULL_STATE, _EMPTY_STATE], ids=["data", "empty"])
async def test_cancel_button_without_a_message_shows_cancel_as_popup_and_sends_nothing(
    dialog: _Dialog, stub: _TrackingStub, fsm: tuple[str | None, dict[str, Any]]
) -> None:
    # Without a message aiogram cannot see the chat, so the FSM key falls back
    # to chat_id == user_id: the draft has to live under that key.
    key = StorageKey(bot_id=dialog.bot.id, chat_id=_USER, user_id=_USER)
    await dialog.dispatcher.storage.set_state(key, fsm[0])
    await dialog.dispatcher.storage.set_data(key, dict(fsm[1]))

    await dialog.press(CB_ADD_CANCEL, with_message=False)

    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.CANCEL_TEXT, True)
    ]
    assert dialog.sent == []
    assert await dialog.dispatcher.storage.get_state(key) is None
    assert await dialog.dispatcher.storage.get_data(key) == {}


@pytest.mark.asyncio
async def test_cancel_clears_state_before_send_so_send_failure_keeps_no_draft() -> None:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=_CHAT, user_id=_USER)
    await storage.set_state(key, AddProduct.confirming.state)
    await storage.set_data(key, dict(_CONFIRMING_DATA))
    state = FSMContext(storage=storage, key=key)
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.answer = AsyncMock(side_effect=RuntimeError("telegram down"))

    with pytest.raises(RuntimeError, match="telegram down"):
        await _handler("on_cancel")(callback, state)

    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}


# --- direction (PAB-072) ----------------------------------------------------


@pytest.mark.asyncio
async def test_threshold_below_the_price_shows_a_below_card_and_saves_the_card_id(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)

    await dialog.say("1500")

    card = dialog.sent[-1]
    assert f"Сообщу, когда цена станет ниже 1{_NBSP}500{_NBSP}₽." in card.text
    assert "выше" not in card.text
    assert PRICE_DISCLAIMER_SHORT in card.text
    assert card.parse_mode is None
    assert await dialog.state() == AddProduct.confirming.state
    assert (await dialog.data())["confirm_message_id"] == sent_message_id(0)


@pytest.mark.asyncio
async def test_threshold_above_the_price_shows_an_above_card_with_disclaimer(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)

    await dialog.say("2500")

    card = dialog.sent[-1]
    assert f"Сообщу, когда цена станет выше 2{_NBSP}500{_NBSP}₽." in card.text
    assert "ниже" not in card.text
    assert PRICE_DISCLAIMER_SHORT in card.text
    assert card.parse_mode is None
    assert _keyboard_callbacks(card) == [CB_ADD_CONFIRM, CB_ADD_CANCEL]
    assert await dialog.state() == AddProduct.confirming.state


@pytest.mark.asyncio
async def test_threshold_one_kopeck_either_side_of_the_price_picks_each_direction(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)
    await dialog.say("1989,99")
    assert "станет ниже" in dialog.last_text

    await _at_waiting_price(dialog)
    await dialog.say("1990,01")
    assert "станет выше" in dialog.last_text


@pytest.mark.asyncio
async def test_threshold_equal_to_the_price_is_refused_and_the_step_holds(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)

    await dialog.say("1990")

    assert dialog.last_text == texts.ADD_TARGET_EQUALS_TEXT
    assert _keyboard_callbacks(dialog.sent[-1]) == [CB_ADD_CANCEL]
    assert await dialog.state() == AddProduct.waiting_target_price.state
    data = await dialog.data()
    assert "target_price" not in data
    assert "confirm_message_id" not in data
    assert stub.add_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "phrase"),
    [
        (AlertDirection.below, "цена станет ниже"),
        (AlertDirection.above, "цена станет выше"),
    ],
)
async def test_success_text_follows_the_direction_of_the_created_alert(
    dialog: _Dialog, stub: _TrackingStub, direction: AlertDirection, phrase: str
) -> None:
    """For ``above`` the stub answers against what the 150_000 target implies,
    so the text must come from the returned alert, not be recomputed."""
    stub.add_direction = direction
    await _at_confirming(dialog)

    await dialog.press(CB_ADD_CONFIRM)

    assert f"Сообщу, когда {phrase} 1{_NBSP}500{_NBSP}₽." in dialog.last_text
    assert PRICE_DISCLAIMER_SHORT in dialog.last_text
    assert dialog.sent[-1].parse_mode is None


@pytest.mark.asyncio
async def test_service_reporting_equal_threshold_gets_a_domain_text_and_clears_state(
    dialog: _Dialog, stub: _TrackingStub, log_records: list[Record]
) -> None:
    stub.add_error = TargetEqualsCurrentPriceError()
    await _at_confirming(dialog)

    await dialog.press(CB_ADD_CONFIRM)

    assert dialog.last_text == texts.ADD_TARGET_EQUALS_RESTART_TEXT
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() is None
    assert [r for r in log_records if r["level"].name == "ERROR"] == []


# --- stale confirmation card (PAB-072) -----------------------------------------


async def _two_cards_by_restart(dialog: _Dialog) -> tuple[int, int]:
    """Run the dialog twice; return the ``message_id`` of both cards."""
    await dialog.command("add")
    await dialog.say(_WB_LINK)
    await dialog.say("1500")
    first = (await dialog.data())["confirm_message_id"]
    await dialog.command("add")
    await dialog.say(_WB_LINK)
    await dialog.say("1400")
    return first, (await dialog.data())["confirm_message_id"]


@pytest.mark.asyncio
async def test_confirm_under_the_first_of_two_cards_is_stale_and_the_second_works(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    first, second = await _two_cards_by_restart(dialog)
    assert first != second
    data_before = await dialog.data()

    await dialog.press(CB_ADD_CONFIRM, message_id=first)

    assert stub.add_calls == []
    assert [a.text for a in dialog.callback_answers] == [texts.ADD_OLD_CARD_ALERT_TEXT]
    assert dialog.callback_answers[0].show_alert is True
    assert await dialog.state() == AddProduct.confirming.state
    assert await dialog.data() == data_before

    await dialog.press(CB_ADD_CONFIRM, message_id=second)

    assert stub.add_calls == [(_USER, Marketplace.wb, 12345678, _NAME, _PRICE, 140_000)]
    assert dialog.last_text.startswith("Отслеживание добавлено")
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_hint_card_replaces_the_live_card_and_the_old_one_turns_stale(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await _at_waiting_price(dialog)
    await dialog.say("1500")
    card_id = (await dialog.data())["confirm_message_id"]

    await dialog.say("yes")

    hint_id = (await dialog.data())["confirm_message_id"]
    assert hint_id == sent_message_id(1)
    assert hint_id != card_id
    await dialog.press(CB_ADD_CONFIRM, message_id=card_id)
    assert stub.add_calls == []
    await dialog.press(CB_ADD_CONFIRM, message_id=hint_id)
    assert len(stub.add_calls) == 1
    assert await dialog.state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "press_kwargs", "expected"),
    [
        (
            {k: v for k, v in _CONFIRMING_DATA.items() if k != "confirm_message_id"},
            {},
            texts.ADD_OLD_CARD_ALERT_TEXT,
        ),
        # No message at all is not routed to the card check: generic stale text.
        (dict(_CONFIRMING_DATA), {"with_message": False}, texts.ADD_STALE_ALERT_TEXT),
        (dict(_CONFIRMING_DATA), {"accessible": False}, texts.ADD_OLD_CARD_ALERT_TEXT),
    ],
    ids=["no-key", "message-none", "inaccessible-message"],
)
async def test_confirm_without_a_verifiable_card_is_stale_and_keeps_the_draft(
    dialog: _Dialog,
    stub: _TrackingStub,
    data: dict[str, Any],
    press_kwargs: dict[str, Any],
    expected: str,
) -> None:
    await dialog.set_state(AddProduct.confirming.state, data)

    await dialog.press(CB_ADD_CONFIRM, **press_kwargs)

    assert stub.add_calls == []
    assert [a.text for a in dialog.callback_answers] == [expected]
    assert dialog.sent == []
    assert await dialog.state() == AddProduct.confirming.state
    assert await dialog.data() == data


@pytest.mark.asyncio
async def test_commit_failure_after_a_successful_confirm_adds_the_error_text(
    stub: _TrackingStub, log_records: list[Any]
) -> None:
    """The callback is already answered; the catch-all still tells the user."""

    class _CommitFails(AsyncSession):
        async def commit(self) -> None:
            raise RuntimeError("COMMIT failed")

    dispatcher = create_dispatcher(
        MemoryStorage(),
        async_sessionmaker(class_=_CommitFails),
        httpx.AsyncClient(),
        50,
        stub_client_factory(),
    )
    dialog = _Dialog(
        dispatcher, build_fake_bot(responder=send_message_responder()), _Faults()
    )
    await _at_confirming(dialog)

    await dialog.press(CB_ADD_CONFIRM)

    assert len(stub.add_calls) == 1
    # One answer from the handler, one from the catch-all (always attempted).
    assert len(dialog.callback_answers) == 2
    texts_sent = [m.text for m in dialog.sent]
    assert len(texts_sent) == 2
    assert texts_sent[0].startswith("Отслеживание добавлено")
    assert texts_sent[1] == texts.UNEXPECTED_ERROR_TEXT


# --- wiring ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracking_service_is_assembled_from_session_and_workflow_data(
    stub: _TrackingStub,
) -> None:
    http_client = httpx.AsyncClient()
    factory = stub_client_factory()
    dispatcher = create_dispatcher(
        MemoryStorage(),
        async_sessionmaker(class_=AsyncSession),
        http_client,
        17,
        factory,
    )
    bot = build_fake_bot()

    await dispatcher.feed_update(
        bot, make_command_update("add", chat_id=_CHAT, user_id=_USER)
    )
    await dispatcher.feed_update(
        bot,
        make_message_update(text=_WB_LINK, update_id=2, chat_id=_CHAT, user_id=_USER),
    )

    assert len(stub.init_kwargs) == 2  # one per update
    for kwargs in stub.init_kwargs:
        assert isinstance(kwargs["session"], AsyncSession)
        assert kwargs["http_client"] is http_client
        assert kwargs["max_products_per_user"] == 17
        # PAB-079: the factory comes from the dispatcher data, not an import.
        assert kwargs["client_factory"] is factory
    await http_client.aclose()


@pytest.mark.asyncio
async def test_unknown_failure_reason_is_logged_as_error_and_not_shown_as_a_normal_text(
    dialog: _Dialog, stub: _TrackingStub, log_records: list[Record]
) -> None:
    stub.preview_result = MarketplaceFetchFailure.model_construct(
        reason=cast(Any, "brand_new_reason")
    )
    await dialog.command("add")

    await dialog.say(_WB_LINK)  # must not propagate: the errors router handles it

    errors = [r for r in log_records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert isinstance(errors[0]["exception"].value, AssertionError)  # type: ignore[union-attr]
    assert dialog.last_text == texts.UNEXPECTED_ERROR_TEXT
    assert await dialog.state() == AddProduct.waiting_link.state


def _handler(name: str) -> Any:
    router = add_product_module.create_router()
    for observer in router.observers.values():
        for handler in observer.handlers:
            if handler.callback.__name__ == name:
                return handler.callback
    raise AssertionError(name)


@pytest.mark.asyncio
async def test_price_handler_ignores_a_message_without_text() -> None:
    """Defensive guard behind the ``F.text`` filter: reached only by a direct call."""
    message = MagicMock(spec=Message)
    message.text = None
    message.answer = AsyncMock()
    state = MagicMock()

    await _handler("on_target_price")(message, state)

    message.answer.assert_not_called()
    assert state.mock_calls == []


@pytest.mark.asyncio
async def test_link_handler_ignores_a_message_without_text() -> None:
    message = MagicMock(spec=Message)
    message.text = None
    message.from_user = MagicMock()
    message.answer = AsyncMock()
    tracking = MagicMock()
    tracking.preview = AsyncMock()

    await _handler("on_link")(message, MagicMock(), tracking)

    message.answer.assert_not_called()
    tracking.preview.assert_not_called()


# --- "checking" notice on the link step (PAB-090) ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", _OUTCOMES)
async def test_notice_is_sent_without_keyboard_before_preview_is_called(
    dialog: _Dialog, stub: _TrackingStub, outcome: _Outcome
) -> None:
    outcome.configure(stub)
    await dialog.command("add")
    base = len(dialog.requests)
    at_preview: list[list[Any]] = []
    stub.on_preview = lambda: at_preview.append(dialog.requests[base:])

    await dialog.say(_WB_LINK)

    assert len(at_preview) == 1
    assert [type(r) for r in at_preview[0]] == [SendMessage]
    notice = at_preview[0][0]
    assert notice.text == texts.ADD_CHECKING_TEXT
    assert notice.reply_markup is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", _OUTCOMES)
async def test_outcome_replaces_the_notice_in_place_and_is_the_last_request(
    dialog: _Dialog, stub: _TrackingStub, outcome: _Outcome
) -> None:
    outcome.configure(stub)
    await dialog.command("add")
    base = len(dialog.requests)

    await dialog.say(_WB_LINK)

    step = dialog.requests[base:]
    assert [type(r) for r in step] == [SendMessage, EditMessageText]
    notice, edit = step
    assert isinstance(notice, SendMessage)
    assert isinstance(edit, EditMessageText)
    assert edit.message_id == sent_message_id(1)  # 0 is the /add prompt
    assert edit.chat_id == _CHAT
    _assert_shown_as(edit, outcome)
    assert await dialog.state() == outcome.state


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["hello", "https://evil.com/x", "0", "   "])
async def test_unparseable_link_sends_only_the_hint_without_notice_or_edit(
    dialog: _Dialog, stub: _TrackingStub, bad: str
) -> None:
    await dialog.command("add")
    base = len(dialog.requests)

    await dialog.say(bad)

    step = dialog.requests[base:]
    assert [type(r) for r in step] == [SendMessage]
    assert step[0].text == texts.ADD_LINK_FORMAT_HINT_TEXT
    assert stub.preview_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", _OUTCOMES)
async def test_failed_notice_does_not_break_the_step_and_outcome_comes_as_new_message(
    dialog: _Dialog,
    stub: _TrackingStub,
    log_records: list[Record],
    outcome: _Outcome,
) -> None:
    outcome.configure(stub)
    await dialog.command("add")
    base = len(dialog.requests)
    dialog.faults.next_send_error = TelegramNetworkError(
        method=SendMessage(chat_id=_CHAT, text="x"), message="SECRET-NET-DETAIL"
    )

    await dialog.say(_WB_LINK)

    step = dialog.requests[base:]
    assert [type(r) for r in step] == [SendMessage, SendMessage]
    assert step[0].text == texts.ADD_CHECKING_TEXT  # the attempt that failed
    assert isinstance(step[1], SendMessage)
    _assert_shown_as(step[1], outcome)
    assert await dialog.state() == outcome.state
    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0]["extra"]["error_type"] == "TelegramNetworkError"
    assert _WB_LINK not in warnings[0]["message"]
    assert "SECRET-NET-DETAIL" not in warnings[0]["message"]
    assert [r for r in log_records if r["level"].name == "ERROR"] == []
    assert texts.UNEXPECTED_ERROR_TEXT not in [s.text for s in dialog.sent]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", _OUTCOMES)
async def test_rejected_edit_delivers_the_same_outcome_as_new_message(
    dialog: _Dialog,
    stub: _TrackingStub,
    log_records: list[Record],
    outcome: _Outcome,
) -> None:
    outcome.configure(stub)
    dialog.faults.edit_error = TelegramBadRequest(
        method=EditMessageText(chat_id=_CHAT, message_id=1, text="x"),
        message="Bad Request: message to edit not found",
    )
    await dialog.command("add")
    base = len(dialog.requests)

    await dialog.say(_WB_LINK)

    step = dialog.requests[base:]
    assert [type(r) for r in step] == [SendMessage, EditMessageText, SendMessage]
    delivered = step[2]
    assert isinstance(delivered, SendMessage)
    _assert_shown_as(delivered, outcome)
    assert await dialog.state() == outcome.state
    assert [r for r in log_records if r["level"].name in ("WARNING", "ERROR")] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", _OUTCOMES)
async def test_network_error_on_edit_is_not_resent_and_goes_to_the_errors_handler(
    dialog: _Dialog,
    stub: _TrackingStub,
    log_records: list[Record],
    outcome: _Outcome,
) -> None:
    outcome.configure(stub)
    dialog.faults.edit_error = TelegramNetworkError(
        method=EditMessageText(chat_id=_CHAT, message_id=1, text="x"), message="down"
    )
    await dialog.command("add")
    base = len(dialog.requests)

    await dialog.say(_WB_LINK)

    sent_texts = [r.text for r in dialog.requests[base:] if isinstance(r, SendMessage)]
    assert outcome.text not in sent_texts
    assert sent_texts == [texts.ADD_CHECKING_TEXT, texts.UNEXPECTED_ERROR_TEXT]
    errors = [r for r in log_records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert isinstance(errors[0]["exception"].value, TelegramNetworkError)  # type: ignore[union-attr]


# --- a late check result must not override the user's last action (PAB-091) ------

_LINK_B = "https://www.wildberries.ru/catalog/22222222/detail.aspx"
_MSG_B = 102  # message_id of the second link message


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> _GatedTracking:
    tracking = _GatedTracking()
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_: tracking)
    return tracking


async def _start_link(
    dialog: _Dialog, call: _Call, link: str, message_id: int
) -> asyncio.Task[None]:
    """Feed a link and return once its ``preview`` is parked inside the double."""
    task = asyncio.create_task(dialog.say(link, message_id=message_id))
    async with asyncio.timeout(_WAIT):
        await call.entered.wait()
    return task


async def _finish_link(task: asyncio.Task[None], call: _Call) -> None:
    assert call.release is not None
    call.release.set()
    async with asyncio.timeout(_WAIT):
        await task


def _checking_notice_id(dialog: _Dialog, nth: int = 0) -> int:
    """``message_id`` the fake Bot API gave the ``nth`` "checking" notice."""
    ids = [
        sent_message_id(i)
        for i, m in enumerate(dialog.sent)
        if m.text == texts.ADD_CHECKING_TEXT
    ]
    return ids[nth]


def _dropped_infos(log_records: list[Record]) -> list[Record]:
    return [
        r
        for r in log_records
        if r["level"].name == "INFO" and r["extra"].get("user_id") == _USER
    ]


async def _press_cancel(dialog: _Dialog) -> None:
    await dialog.press(CB_ADD_CANCEL)


async def _send_cancel_command(dialog: _Dialog) -> None:
    await dialog.command("cancel")


async def _send_add_command(dialog: _Dialog) -> None:
    await dialog.command("add")


async def _press_menu_add(dialog: _Dialog) -> None:
    await dialog.press("menu:add")


_INTERRUPTS = [
    pytest.param(_press_cancel, None, "state_changed", id="cancel-button"),
    pytest.param(_send_cancel_command, None, "state_changed", id="cancel-command"),
    pytest.param(
        _send_add_command,
        AddProduct.waiting_link.state,
        "marker_mismatch",
        id="add-again",
    ),
    pytest.param(
        _press_menu_add,
        AddProduct.waiting_link.state,
        "marker_mismatch",
        id="menu-add-button",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("interrupt", "state_after", "reason"), _INTERRUPTS)
async def test_interrupt_during_check_wins_and_late_result_only_retitles_the_notice(
    dialog: _Dialog,
    gated: _GatedTracking,
    interrupt: Callable[[_Dialog], Any],
    state_after: str | None,
    reason: str,
    log_records: list[Record],
) -> None:
    call = gated.script(0)
    await dialog.command("add")
    task = await _start_link(dialog, call, _LINK_A, _MSG_A)
    notice_id = _checking_notice_id(dialog)

    await interrupt(dialog)
    base = len(dialog.requests)
    await _finish_link(task, call)

    assert await dialog.state() == state_after
    assert await dialog.data() == {}
    _assert_only_dropped_edit(dialog, base, notice_id)
    assert [r["extra"]["reason"] for r in _dropped_infos(log_records)] == [reason]


@pytest.mark.asyncio
async def test_second_link_finishing_first_wins_and_first_is_dropped(
    dialog: _Dialog, gated: _GatedTracking
) -> None:
    call_a = gated.script(0, result=MarketplaceProductData(name=_NAME_A, price=111))
    call_b = gated.script(1, result=MarketplaceProductData(name=_NAME_B, price=222))
    await dialog.command("add")
    task_a = await _start_link(dialog, call_a, _LINK_A, _MSG_A)
    task_b = await _start_link(dialog, call_b, _LINK_B, _MSG_B)
    notice_a, notice_b = _checking_notice_id(dialog, 0), _checking_notice_id(dialog, 1)

    await _finish_link(task_b, call_b)
    base = len(dialog.requests)
    await _finish_link(task_a, call_a)

    _assert_only_dropped_edit(dialog, base, notice_a)
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert await dialog.data() == {
        "marketplace": "wb",
        "article": 22222222,
        "product_name": _NAME_B,
        "current_price": 222,
        "link_message_id": _MSG_B,
    }
    card = [(e.message_id, e.text) for e in dialog.edits if _NAME_B in e.text]
    assert card == [(notice_b, texts.product_found_text(_NAME_B, 222))]
    assert _NAME_A not in _shown_texts(dialog)


@pytest.mark.asyncio
async def test_first_link_finishing_first_is_dropped_by_foreign_marker_second_applies(
    dialog: _Dialog, gated: _GatedTracking, log_records: list[Record]
) -> None:
    call_a = gated.script(0, result=MarketplaceProductData(name=_NAME_A, price=111))
    call_b = gated.script(1, result=MarketplaceProductData(name=_NAME_B, price=222))
    await dialog.command("add")
    task_a = await _start_link(dialog, call_a, _LINK_A, _MSG_A)
    task_b = await _start_link(dialog, call_b, _LINK_B, _MSG_B)
    notice_a, notice_b = _checking_notice_id(dialog, 0), _checking_notice_id(dialog, 1)

    base = len(dialog.requests)
    await _finish_link(task_a, call_a)

    _assert_only_dropped_edit(dialog, base, notice_a)
    assert [r["extra"]["reason"] for r in _dropped_infos(log_records)] == [
        "marker_mismatch"
    ]
    assert await dialog.state() == AddProduct.waiting_link.state
    assert await dialog.data() == {"link_message_id": _MSG_B}

    await _finish_link(task_b, call_b)

    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert (await dialog.data())["product_name"] == _NAME_B
    assert (await dialog.data())["link_message_id"] == _MSG_B
    last = dialog.outcome
    assert isinstance(last, EditMessageText)
    assert last.message_id == notice_b
    assert _NAME_B in last.text
    assert _NAME_A not in _shown_texts(dialog)


@pytest.mark.asyncio
async def test_result_is_dropped_when_state_moved_on_even_if_the_marker_is_still_ours(
    dialog: _Dialog, gated: _GatedTracking
) -> None:
    call = gated.script(0)
    await dialog.command("add")
    task = await _start_link(dialog, call, _LINK_A, _MSG_A)
    notice_id = _checking_notice_id(dialog)
    moved = {"link_message_id": _MSG_A}
    await dialog.set_state(AddProduct.waiting_target_price.state, moved)

    base = len(dialog.requests)
    await _finish_link(task, call)

    _assert_only_dropped_edit(dialog, base, notice_id)
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert await dialog.data() == moved


_LATE_OUTCOMES = [
    pytest.param({"error": ProductLimitExceededError(37)}, id="limit"),
    pytest.param({"error": MarketplaceNotSupportedError("ozon")}, id="unsupported"),
    pytest.param(
        {
            "result": MarketplaceFetchFailure(
                reason=FetchFailureReason.blocked, detail="d"
            )
        },
        id="fetch-failure",
    ),
    pytest.param(
        {"result": MarketplaceProductData(name=_NAME_A, price=111)}, id="success"
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scripted", _LATE_OUTCOMES)
async def test_late_outcome_of_every_branch_writes_no_fsm_and_shows_no_outcome(
    dialog: _Dialog, gated: _GatedTracking, scripted: dict[str, Any]
) -> None:
    call = gated.script(0, **scripted)
    await dialog.command("add")
    task = await _start_link(dialog, call, _LINK_A, _MSG_A)
    notice_id = _checking_notice_id(dialog)
    await dialog.command("add")  # restart: state waiting_link, data empty
    base = len(dialog.requests)

    await _finish_link(task, call)

    # a state.clear() in the limit branch would leave None here
    assert await dialog.state() == AddProduct.waiting_link.state
    assert await dialog.data() == {}
    _assert_only_dropped_edit(dialog, base, notice_id)


_EDIT_FAILURES = [
    pytest.param(
        TelegramBadRequest(
            method=EditMessageText(chat_id=_CHAT, message_id=1, text="x"),
            message="Bad Request: SECRET-EDIT-DETAIL",
        ),
        "TelegramBadRequest",
        id="bad-request",
    ),
    pytest.param(
        TelegramNetworkError(
            method=EditMessageText(chat_id=_CHAT, message_id=1, text="x"),
            message="SECRET-EDIT-DETAIL",
        ),
        "TelegramNetworkError",
        id="network",
    ),
    pytest.param(RuntimeError("SECRET-EDIT-DETAIL"), "RuntimeError", id="other"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "error_type"), _EDIT_FAILURES)
async def test_failed_retitle_of_dropped_notice_is_swallowed_with_one_warning(
    dialog: _Dialog,
    gated: _GatedTracking,
    log_records: list[Record],
    failure: Exception,
    error_type: str,
) -> None:
    call = gated.script(0)
    await dialog.command("add")
    task = await _start_link(dialog, call, _LINK_A, _MSG_A)
    await dialog.command("cancel")
    dialog.faults.edit_error = failure
    base = len(dialog.requests)

    await _finish_link(task, call)  # must not raise

    step = dialog.requests[base:]
    assert [type(r) for r in step] == [EditMessageText]  # attempted, no new message
    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0]["extra"]["error_type"] == error_type
    assert "SECRET-EDIT-DETAIL" not in warnings[0]["message"]
    assert [r for r in log_records if r["level"].name == "ERROR"] == []
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_dropped_check_without_a_notice_makes_no_bot_api_call(
    dialog: _Dialog, gated: _GatedTracking, log_records: list[Record]
) -> None:
    call = gated.script(0)
    await dialog.command("add")
    dialog.faults.next_send_error = TelegramNetworkError(
        method=SendMessage(chat_id=_CHAT, text="x"), message="down"
    )
    task = await _start_link(dialog, call, _LINK_A, _MSG_A)
    await dialog.command("cancel")
    base = len(dialog.requests)

    await _finish_link(task, call)

    assert dialog.requests[base:] == []
    assert len(_dropped_infos(log_records)) == 1


@pytest.mark.asyncio
async def test_dropped_check_logs_exactly_one_info_with_user_id_and_no_link(
    dialog: _Dialog, gated: _GatedTracking, log_records: list[Record]
) -> None:
    call = gated.script(0)
    await dialog.command("add")
    task = await _start_link(dialog, call, _LINK_A, _MSG_A)
    await dialog.command("cancel")
    assert _dropped_infos(log_records) == []

    await _finish_link(task, call)

    infos = _dropped_infos(log_records)
    assert len(infos) == 1
    assert "11111111" not in infos[0]["message"]


@pytest.mark.asyncio
async def test_current_check_logs_no_dropped_info_and_keeps_the_pab090_request_count(
    dialog: _Dialog, gated: _GatedTracking, log_records: list[Record]
) -> None:
    call = gated.script(0, gated=False)
    await dialog.command("add")
    base = len(dialog.requests)

    task = await _start_link(dialog, call, _LINK_A, _MSG_A)
    async with asyncio.timeout(_WAIT):
        await task

    assert [type(r) for r in dialog.requests[base:]] == [SendMessage, EditMessageText]
    assert _dropped_infos(log_records) == []
    assert (await dialog.data())["link_message_id"] == _MSG_A


# --- main menu on the final screens, "add" menu button (PAB-092) ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "data"),
    [
        pytest.param(None, {}, id="no-dialog"),
        pytest.param(AddProduct.waiting_link.state, {}, id="waiting-link"),
        pytest.param(
            AddProduct.waiting_link.state,
            {"link_message_id": _MSG_A},
            id="waiting-link-with-pab091-marker",
        ),
        pytest.param(
            AddProduct.waiting_target_price.state,
            dict(_CONFIRMING_DATA),
            id="waiting-price",
        ),
        pytest.param(
            AddProduct.confirming.state, dict(_CONFIRMING_DATA), id="confirming"
        ),
    ],
)
async def test_menu_add_button_restarts_the_dialog_in_place_from_any_state(
    dialog: _Dialog, stub: _TrackingStub, state: str | None, data: dict[str, Any]
) -> None:
    await dialog.set_state(state, data)

    await dialog.press("menu:add")

    assert await dialog.state() == AddProduct.waiting_link.state
    assert await dialog.data() == {}
    assert dialog.sent == []
    assert [(e.text, e.message_id, e.parse_mode) for e in dialog.edits] == [
        (texts.ADD_PROMPT_LINK_TEXT, _CARD_ID, None)
    ]
    assert _keyboard_callbacks(dialog.edits[0]) == [CB_ADD_CANCEL]
    assert len(dialog.callback_answers) == 1


@pytest.mark.asyncio
async def test_menu_add_button_on_an_inaccessible_message_sends_the_prompt_as_new(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.press("menu:add", accessible=False)

    assert await dialog.state() == AddProduct.waiting_link.state
    assert dialog.edits == []
    assert [m.text for m in dialog.sent] == [texts.ADD_PROMPT_LINK_TEXT]
    assert _keyboard_callbacks(dialog.sent[0]) == [CB_ADD_CANCEL]
    assert len(dialog.callback_answers) == 1


@pytest.mark.asyncio
async def test_menu_add_button_without_a_message_still_opens_the_dialog_with_a_popup(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    # No message: aiogram falls back to the chat_id == user_id FSM key.
    key = StorageKey(bot_id=dialog.bot.id, chat_id=_USER, user_id=_USER)

    await dialog.press("menu:add", with_message=False)

    assert (
        await dialog.dispatcher.storage.get_state(key) == AddProduct.waiting_link.state
    )
    assert dialog.sent == []
    assert dialog.edits == []
    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.MENU_STALE_ALERT_TEXT, True)
    ]


@pytest.mark.asyncio
async def test_link_after_menu_add_button_starts_a_normal_check(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.press("menu:add")

    await dialog.say(_WB_LINK)

    assert stub.preview_calls == [(_USER, Marketplace.wb, 12345678)]
    assert await dialog.state() == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, None),
        (ProductLimitExceededError(37), texts.limit_exceeded_text(37)),
        (MarketplaceNotSupportedError("ozon"), texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT),
        (TargetEqualsCurrentPriceError(), texts.ADD_TARGET_EQUALS_RESTART_TEXT),
    ],
    ids=["success", "limit", "unsupported", "equals-price"],
)
async def test_every_confirm_outcome_carries_the_main_menu(
    dialog: _Dialog, stub: _TrackingStub, error: Exception | None, expected: str | None
) -> None:
    stub.add_error = error
    await _at_confirming(dialog)

    await dialog.press(CB_ADD_CONFIRM)

    assert _keyboard_callbacks(dialog.sent[-1]) == _MAIN_MENU
    if expected is not None:
        assert dialog.sent[-1].text == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("state", _STEPS)
async def test_cancel_button_outcome_carries_the_main_menu(
    dialog: _Dialog, stub: _TrackingStub, state: str
) -> None:
    await dialog.set_state(state, dict(_CONFIRMING_DATA))

    await dialog.press(CB_ADD_CANCEL)

    assert dialog.sent[-1].text == texts.CANCEL_TEXT
    assert _keyboard_callbacks(dialog.sent[-1]) == _MAIN_MENU


@pytest.mark.asyncio
async def test_stale_draft_on_the_confirm_step_carries_the_main_menu(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.set_state(
        AddProduct.confirming.state, {"confirm_message_id": _CARD_ID}
    )

    await dialog.press(CB_ADD_CONFIRM)

    assert dialog.sent[-1].text == texts.ADD_STALE_TEXT
    assert _keyboard_callbacks(dialog.sent[-1]) == _MAIN_MENU


@pytest.mark.asyncio
async def test_stale_draft_on_the_price_step_carries_the_main_menu(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.set_state(AddProduct.waiting_target_price.state, {})

    await dialog.say("1500")

    assert dialog.sent[-1].text == texts.ADD_STALE_TEXT
    assert _keyboard_callbacks(dialog.sent[-1]) == _MAIN_MENU


@pytest.mark.asyncio
async def test_input_steps_carry_only_cancel_or_confirm_buttons(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CANCEL]
    await dialog.say("hello")  # format hint
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CANCEL]
    await dialog.say(_WB_LINK)  # product card
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CANCEL]
    await dialog.say("0")  # price hint
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CANCEL]
    await dialog.say("1500")  # confirmation card
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CONFIRM, CB_ADD_CANCEL]
    await dialog.say("yes")  # confirm hint
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CONFIRM, CB_ADD_CANCEL]
