"""Tests for the "add this product" button, app.handlers.add_product (PAB-094).

The button (``AddRefCb``) restarts the /add dialog at the link step with an
already parsed reference. Everything goes through ``Dispatcher.feed_update``;
the replaced boundary is ``TrackingService``. Race scenarios follow the
PAB-091 pattern: ``preview`` parks on an ``asyncio.Event``, never on a sleep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import TelegramMethod

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.states import AddProduct
from app.domain.exceptions import ProductLimitExceededError
from app.models.enums import Marketplace
from app.schemas.marketplace import MarketplaceProductData
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.conftest import make_callback_update
from tests.unit.conftest import sent_message_id
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _CONFIRMING_DATA
from tests.unit.handlers.add_dialog_support import _LINK_A
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
from tests.unit.handlers.add_dialog_support import _Outcome
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.add_dialog_support import _shown_texts
from tests.unit.handlers.add_dialog_support import _TrackingStub
from tests.unit.handlers.add_dialog_support import _without_stamp

_WB_PAYLOAD = "addref:wb:12345678"
_BUTTON_ID = _CARD_ID  # message_id of the message carrying the button


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _TrackingStub:
    tracking = _TrackingStub()
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_: tracking)
    return tracking


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> _GatedTracking:
    tracking = _GatedTracking()
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_: tracking)
    return tracking


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


def _types(dialog: _Dialog) -> list[type]:
    return [type(r) for r in dialog.requests]


# --- the four outcomes, same as /add by link ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", _OUTCOMES)
async def test_button_shows_checking_then_the_link_step_outcome_by_editing_in_place(
    dialog: _Dialog, stub: _TrackingStub, outcome: _Outcome
) -> None:
    outcome.configure(stub)

    await dialog.press(_WB_PAYLOAD)

    assert stub.preview_calls == [(_USER, Marketplace.wb, 12345678)]
    assert dialog.sent == []
    checking, shown = dialog.edits
    assert (checking.chat_id, checking.message_id) == (_CHAT, _BUTTON_ID)
    assert checking.text == texts.ADD_CHECKING_TEXT
    assert checking.reply_markup is None
    assert (shown.chat_id, shown.message_id) == (_CHAT, _BUTTON_ID)
    _assert_shown_as(shown, outcome)
    assert await dialog.state() == outcome.state


@pytest.mark.asyncio
async def test_button_card_is_stored_in_fsm_with_the_pressed_message_as_marker(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    before = datetime.now(UTC)
    await dialog.press("addref:ozon:1234567")
    after = datetime.now(UTC)

    assert stub.preview_calls == [(_USER, Marketplace.ozon, 1234567)]
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert _without_stamp(await dialog.data(), before, after) == {
        "marketplace": "ozon",
        "article": 1234567,
        "product_name": _NAME,
        "current_price": _PRICE,
        "link_message_id": _BUTTON_ID,
    }


@pytest.mark.asyncio
async def test_button_answers_the_callback_exactly_once_and_before_preview(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    answered_at_preview: list[int] = []
    stub.on_preview = lambda: answered_at_preview.append(
        sum(isinstance(r, AnswerCallbackQuery) for r in dialog.requests)
    )

    await dialog.press(_WB_PAYLOAD)

    assert answered_at_preview == [1]
    assert len(dialog.callback_answers) == 1
    assert (dialog.callback_answers[0].text, dialog.callback_answers[0].show_alert) == (
        None,
        None,
    )


@pytest.mark.asyncio
async def test_button_on_a_message_already_showing_the_checking_text_does_not_re_edit(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.feed(
        make_callback_update(
            _WB_PAYLOAD,
            chat_id=_CHAT,
            user_id=_USER,
            message_id=_BUTTON_ID,
            message_text=texts.ADD_CHECKING_TEXT,
        )
    )

    assert stub.preview_calls == [(_USER, Marketplace.wb, 12345678)]
    assert [e.text for e in dialog.edits] == [texts.product_found_text(_NAME, _PRICE)]
    assert dialog.sent == []
    assert len(dialog.callback_answers) == 1


# --- unusable button messages ---------------------------------------------------


@pytest.mark.asyncio
async def test_button_without_a_message_pops_up_the_stale_alert_and_changes_nothing(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    # No message: aiogram falls back to the chat_id == user_id FSM key.
    key = StorageKey(bot_id=dialog.bot.id, chat_id=_USER, user_id=_USER)
    await dialog.dispatcher.storage.set_state(key, AddProduct.confirming.state)
    await dialog.dispatcher.storage.set_data(key, dict(_CONFIRMING_DATA))

    await dialog.press(_WB_PAYLOAD, with_message=False)

    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.ADD_STALE_ALERT_TEXT, True)
    ]
    assert _types(dialog) == [AnswerCallbackQuery]
    assert stub.preview_calls == []
    storage = dialog.dispatcher.storage
    assert await storage.get_state(key) == AddProduct.confirming.state
    assert await storage.get_data(key) == _CONFIRMING_DATA


@pytest.mark.asyncio
async def test_button_on_an_inaccessible_message_sends_the_notice_as_a_new_message(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.press(_WB_PAYLOAD, accessible=False)

    assert stub.preview_calls == [(_USER, Marketplace.wb, 12345678)]
    assert [m.text for m in dialog.sent] == [texts.ADD_CHECKING_TEXT]
    assert [(e.message_id, e.text) for e in dialog.edits] == [
        (1000, texts.product_found_text(_NAME, _PRICE))
    ]
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
async def test_refused_edit_sends_new_notice_and_edits_that_notice_with_the_outcome(
    real_dispatcher: Dispatcher, stub: _TrackingStub
) -> None:
    faults = _Faults()
    base = _responder(faults)

    async def refuse_only_the_button_edit(method: TelegramMethod[Any]) -> Any:
        if isinstance(method, EditMessageText) and method.message_id == _BUTTON_ID:
            raise TelegramBadRequest(
                method=method, message="Bad Request: message can't be edited"
            )
        return await base(method)

    bot = Bot(token="1:test-token", session=_BoundSession(refuse_only_the_button_edit))
    bot._me = FAKE_BOT_USER
    dialog = _Dialog(real_dispatcher, bot, faults)

    await dialog.press(_WB_PAYLOAD)

    # The refused attempt (control: the fault really fired) is the only edit
    # of the button message; the notice is one new message.
    assert [e.text for e in dialog.edits if e.message_id == _BUTTON_ID] == [
        texts.ADD_CHECKING_TEXT
    ]
    assert [m.text for m in dialog.sent] == [texts.ADD_CHECKING_TEXT]
    notice_id = sent_message_id(0)
    assert [
        (e.chat_id, e.message_id, e.text)
        for e in dialog.edits
        if e.message_id != _BUTTON_ID
    ] == [(_CHAT, notice_id, texts.product_found_text(_NAME, _PRICE))]
    assert len(dialog.callback_answers) == 1
    assert await dialog.state() == AddProduct.waiting_target_price.state


# --- forged payloads --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("addref:wb:0", id="article-zero"),
        pytest.param("addref:wb:9223372036854775808", id="article-above-max"),
        pytest.param("addref:wb:-5", id="article-negative"),
        pytest.param("addref:nope:5", id="unknown-marketplace"),
        pytest.param("addref:wb", id="missing-part"),
        pytest.param("addref:wb:5:7", id="extra-part"),
        pytest.param("addref", id="prefix-only"),
    ],
)
async def test_forged_payload_pops_up_stale_alert_and_never_reaches_the_service(
    dialog: _Dialog, stub: _TrackingStub, payload: str
) -> None:
    await dialog.set_state(AddProduct.confirming.state, dict(_CONFIRMING_DATA))

    await dialog.press(payload)

    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.ADD_STALE_ALERT_TEXT, True)
    ]
    assert _types(dialog) == [AnswerCallbackQuery]
    assert stub.preview_calls == []
    assert await dialog.state() == AddProduct.confirming.state
    assert await dialog.data() == _CONFIRMING_DATA


# --- limit -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_at_the_product_limit_shows_the_limit_and_clears_the_dialog(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_error = ProductLimitExceededError(37)

    await dialog.press(_WB_PAYLOAD)

    assert dialog.edits[-1].text == texts.limit_exceeded_text(37)
    assert await dialog.state() is None
    assert await dialog.data() == {}


# --- races (PAB-091 pattern) -------------------------------------------------------

_BUTTON_B_ID = 7


async def _park(coro: Any, call: _Call) -> asyncio.Task[None]:
    task = asyncio.create_task(coro)
    async with asyncio.timeout(_WAIT):
        await call.entered.wait()
    return task


async def _release(task: asyncio.Task[None], call: _Call) -> None:
    assert call.release is not None
    call.release.set()
    async with asyncio.timeout(_WAIT):
        await task


def _scripted(gated: _GatedTracking) -> tuple[_Call, _Call]:
    return (
        gated.script(0, result=MarketplaceProductData(name=_NAME_A, price=111)),
        gated.script(1, result=MarketplaceProductData(name=_NAME_B, price=222)),
    )


@pytest.mark.asyncio
async def test_button_pressed_during_a_link_check_wins_when_the_link_finishes_last(
    dialog: _Dialog, gated: _GatedTracking
) -> None:
    call_link, call_button = _scripted(gated)
    await dialog.command("add")
    link = await _park(dialog.say(_LINK_A, message_id=_MSG_A), call_link)
    link_notice = 1001  # second send: prompt of /add is the first
    button = await _park(dialog.press("addref:wb:22222222"), call_button)

    await _release(button, call_button)
    base = len(dialog.requests)
    await _release(link, call_link)

    _assert_only_dropped_edit(dialog, base, link_notice)
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert (await dialog.data())["product_name"] == _NAME_B
    assert (await dialog.data())["link_message_id"] == _BUTTON_ID
    assert _NAME_A not in _shown_texts(dialog)
    assert texts.product_found_text(_NAME_B, 222) in _shown_texts(dialog)


@pytest.mark.asyncio
async def test_button_pressed_during_a_link_check_wins_when_the_link_finishes_first(
    dialog: _Dialog, gated: _GatedTracking
) -> None:
    call_link, call_button = _scripted(gated)
    await dialog.command("add")
    link = await _park(dialog.say(_LINK_A, message_id=_MSG_A), call_link)
    link_notice = 1001
    button = await _park(dialog.press("addref:wb:22222222"), call_button)

    base = len(dialog.requests)
    await _release(link, call_link)

    _assert_only_dropped_edit(dialog, base, link_notice)
    assert await dialog.state() == AddProduct.waiting_link.state
    assert await dialog.data() == {"link_message_id": _BUTTON_ID}

    await _release(button, call_button)

    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert (await dialog.data())["product_name"] == _NAME_B
    assert _NAME_A not in _shown_texts(dialog)
    last = dialog.outcome
    assert isinstance(last, EditMessageText)
    assert (last.message_id, last.text) == (
        _BUTTON_ID,
        texts.product_found_text(_NAME_B, 222),
    )


async def _cancel_button(dialog: _Dialog) -> None:
    await dialog.press(CB_ADD_CANCEL, message_id=_BUTTON_B_ID)


async def _cancel_command(dialog: _Dialog) -> None:
    await dialog.command("cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel",
    [
        pytest.param(_cancel_button, id="cancel-button"),
        pytest.param(_cancel_command, id="cancel-command"),
    ],
)
async def test_cancel_after_the_button_drops_the_outcome_and_retitles_the_notice(
    dialog: _Dialog, gated: _GatedTracking, cancel: Callable[[_Dialog], Any]
) -> None:
    call, _ = _scripted(gated)
    button = await _park(dialog.press(_WB_PAYLOAD), call)

    await cancel(dialog)
    base = len(dialog.requests)
    await _release(button, call)

    _assert_only_dropped_edit(dialog, base, _BUTTON_ID)
    assert await dialog.state() is None
    assert await dialog.data() == {}
    assert _NAME_A not in _shown_texts(dialog)


@pytest.mark.asyncio
async def test_button_from_the_confirming_step_replaces_the_other_products_draft(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.set_state(AddProduct.confirming.state, dict(_CONFIRMING_DATA))

    before = datetime.now(UTC)
    await dialog.press("addref:ozon:777")
    after = datetime.now(UTC)

    assert stub.preview_calls == [(_USER, Marketplace.ozon, 777)]
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert _without_stamp(await dialog.data(), before, after) == {
        "marketplace": "ozon",
        "article": 777,
        "product_name": _NAME,
        "current_price": _PRICE,
        "link_message_id": _BUTTON_ID,
    }
