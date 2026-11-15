"""The /add dialog for a product that has no price to track (PAB-136).

The marketplace identified the product, but no size has a price. The dialog
must not dead-end: the amount is still asked, then the direction is chosen
explicitly, because an absent price cannot determine it.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram import Dispatcher

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import AddDirectionCb
from app.bot.keyboards import direction_keyboard
from app.bot.states import AddProduct
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from app.services.tracking import ArticlePreview
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.conftest import sent_message_id
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.add_dialog_support import _TrackingStub

_WB_LINK = "https://www.wildberries.ru/catalog/12345678/detail.aspx"
_SIZED_LINK = _WB_LINK + "?size=7"
_NAME = "Кроссовки без наличия"
_NBSP = "\u00a0"

_UNAVAILABLE = MarketplaceFetchFailure(
    reason=FetchFailureReason.out_of_stock,
    detail="no size has a price",
    product_name=_NAME,
)


def _unavailable_stub(stub: _TrackingStub) -> _TrackingStub:
    stub.preview_result = _UNAVAILABLE
    return stub


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _TrackingStub:
    """``TrackingService`` double, wired the way the handler builds it."""
    tracking = _TrackingStub()
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: tracking)
    return tracking


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


def _callbacks(markup: Any) -> list[str | None]:
    assert markup is not None
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_link_of_an_unavailable_product_asks_for_the_amount(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    _unavailable_stub(stub)
    await dialog.command("add")

    await dialog.say(_WB_LINK, message_id=77)

    card = dialog.outcome
    assert card.text == texts.product_unavailable_text(Marketplace.wb, _NAME)
    assert _NAME in card.text
    assert "₽" not in card.text
    assert await dialog.state() == AddProduct.waiting_target_price.state
    data = await dialog.data()
    assert data["price_unknown"] is True
    assert data["current_price"] == 0
    assert data["product_name"] == _NAME


@pytest.mark.asyncio
async def test_sized_link_keeps_the_size_in_the_unavailable_draft(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    """The service projects a selected size onto the failure; the dialog keeps
    the option id so the tracked product is the one the user picked."""
    stub.preview_result = _UNAVAILABLE
    await dialog.command("add")

    await dialog.say(_SIZED_LINK, message_id=77)

    data = await dialog.data()
    assert stub.preview_sizes == [7]
    assert data["price_unknown"] is True
    assert data["wb_size_id"] == 7
    assert data["wb_size_name"] is None


@pytest.mark.asyncio
async def test_amount_then_direction_then_confirm_adds_with_the_chosen_direction(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    _unavailable_stub(stub)
    await dialog.command("add")
    await dialog.say(_WB_LINK, message_id=77)

    await dialog.say("1500")

    prompt = dialog.outcome
    assert prompt.text == texts.ADD_DIRECTION_PROMPT_TEXT
    assert _callbacks(prompt.reply_markup) == [
        AddDirectionCb(direction=AlertDirection.below).pack(),
        AddDirectionCb(direction=AlertDirection.above).pack(),
        CB_ADD_CANCEL,
    ]
    assert await dialog.state() == AddProduct.waiting_direction.state

    await dialog.press(
        AddDirectionCb(direction=AlertDirection.above).pack(),
        message_id=sent_message_id(1),
    )

    confirmation = dialog.outcome
    assert confirmation.text == texts.confirm_text(
        _NAME,
        0,
        150_000,
        AlertDirection.above,
        price_unknown=True,
    )
    assert texts.ADD_UNKNOWN_PRICE_LINE in confirmation.text
    assert "Текущая цена" not in confirmation.text
    assert await dialog.state() == AddProduct.confirming.state

    await dialog.press(CB_ADD_CONFIRM, message_id=sent_message_id(1))

    assert stub.add_calls == [(_USER, Marketplace.wb, 12345678, _NAME, 0, 150_000)]
    assert stub.add_directions == [AlertDirection.above]
    final = dialog.outcome
    assert final.text == texts.added_text(
        _NAME, 150_000, AlertDirection.above, price_unknown=True
    )
    assert "Текущая цена" not in final.text
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_text_in_the_direction_step_repeats_the_prompt_and_keeps_the_step(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    _unavailable_stub(stub)
    await dialog.command("add")
    await dialog.say(_WB_LINK, message_id=77)
    await dialog.say("1500")

    await dialog.say("выше")

    assert dialog.last_text == texts.ADD_DIRECTION_PROMPT_TEXT
    assert await dialog.state() == AddProduct.waiting_direction.state
    assert stub.add_calls == []


@pytest.mark.asyncio
async def test_direction_pressed_outside_its_step_is_stale_and_writes_nothing(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")
    await dialog.say(_WB_LINK, message_id=77)

    await dialog.press(AddDirectionCb(direction=AlertDirection.below).pack())

    assert stub.add_calls == []
    assert dialog.callback_answers[-1].show_alert is True
    assert dialog.callback_answers[-1].text == texts.ADD_STALE_ALERT_TEXT


@pytest.mark.asyncio
async def test_confirm_without_a_chosen_direction_is_stale(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    _unavailable_stub(stub)
    await dialog.command("add")
    await dialog.say(_WB_LINK, message_id=77)
    await dialog.say("1500")
    await dialog.set_state(
        AddProduct.confirming.state,
        {
            "marketplace": "wb",
            "article": 12345678,
            "product_name": _NAME,
            "current_price": 0,
            "target_price": 150_000,
            "price_unknown": True,
            "direction": None,
            "confirm_message_id": 5,
        },
    )

    await dialog.press(CB_ADD_CONFIRM, message_id=5)

    assert stub.add_calls == []
    assert dialog.outcome.text == texts.ADD_STALE_TEXT


@pytest.mark.asyncio
async def test_cancel_from_the_direction_step_ends_the_dialog(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    _unavailable_stub(stub)
    await dialog.command("add")
    await dialog.say(_WB_LINK, message_id=77)
    await dialog.say("1500")

    await dialog.press(CB_ADD_CANCEL, message_id=sent_message_id(1))

    assert dialog.outcome.text == texts.CANCEL_TEXT
    assert await dialog.state() is None
    assert stub.add_calls == []


@pytest.mark.asyncio
async def test_not_found_still_refuses_instead_of_offering_tracking(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_result = MarketplaceFetchFailure(
        reason=FetchFailureReason.not_found, detail="empty products"
    )
    await dialog.command("add")

    await dialog.say(_WB_LINK, message_id=77)

    assert (
        dialog.outcome.text
        == texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.not_found]
    )
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_direction_keyboard_offers_both_directions_and_cancel() -> None:
    assert _callbacks(direction_keyboard()) == [
        AddDirectionCb(direction=AlertDirection.below).pack(),
        AddDirectionCb(direction=AlertDirection.above).pack(),
        CB_ADD_CANCEL,
    ]


@pytest.mark.asyncio
async def test_draft_of_a_known_price_product_has_no_unknown_flag(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stamp = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    stub.preview_result = MarketplaceProductData(name="Item", price=199_000)
    await dialog.command("add")

    await dialog.say(_WB_LINK, message_id=77)

    data = await dialog.data()
    assert data["price_unknown"] is False
    assert data["direction"] is None
    assert data["current_price"] == 199_000
    assert stamp.year == 2026


@pytest.mark.asyncio
async def test_direction_on_a_draft_without_the_unknown_flag_is_stale(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.set_state(
        AddProduct.waiting_direction.state,
        {
            "marketplace": "wb",
            "article": 12345678,
            "product_name": _NAME,
            "current_price": 0,
            "target_price": 150_000,
            "price_unknown": False,
            "wb_size_id": 0,
            "wb_size_name": None,
        },
    )

    await dialog.press(AddDirectionCb(direction=AlertDirection.below).pack())

    assert dialog.outcome.text == texts.ADD_STALE_TEXT
    assert await dialog.state() is None
    assert stub.add_calls == []


@pytest.mark.asyncio
async def test_confirm_with_an_unknown_direction_value_is_stale(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.set_state(
        AddProduct.confirming.state,
        {
            "marketplace": "wb",
            "article": 12345678,
            "product_name": _NAME,
            "current_price": 0,
            "target_price": 150_000,
            "price_unknown": True,
            "direction": "sideways",
            "confirm_message_id": 5,
        },
    )

    await dialog.press(CB_ADD_CONFIRM, message_id=5)

    assert dialog.outcome.text == texts.ADD_STALE_TEXT
    assert await dialog.state() is None
    assert stub.add_calls == []


@pytest.mark.asyncio
async def test_bare_article_of_an_unavailable_product_offers_tracking(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    """A bare article found on WB without a price is still a product, not a
    refusal: the searched marketplace is named and the dialog goes on."""
    stub.article_preview = ArticlePreview(Marketplace.wb, _UNAVAILABLE)
    await dialog.command("add")

    await dialog.say("860043555", message_id=77)

    card = dialog.outcome
    assert card.text == texts.product_unavailable_text(Marketplace.wb, _NAME)
    assert await dialog.state() == AddProduct.waiting_target_price.state
    data = await dialog.data()
    assert data["price_unknown"] is True
    assert data["marketplace"] == "wb"
    assert data["article"] == 860043555


@pytest.mark.asyncio
async def test_a_refusal_carrying_a_name_still_refuses(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    """The offer needs an out-of-stock product; a name attached to another
    category must not turn a refusal into a draft."""
    stub.preview_result = MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="HTTP 403", product_name=_NAME
    )
    await dialog.command("add")

    await dialog.say(_WB_LINK, message_id=77)

    assert (
        dialog.outcome.text == texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.blocked]
    )
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_malformed_direction_payload_is_stale(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")

    await dialog.press("adddir:sideways")

    assert dialog.callback_answers[-1].show_alert is True
    assert dialog.callback_answers[-1].text == texts.ADD_STALE_ALERT_TEXT
    assert stub.add_calls == []
