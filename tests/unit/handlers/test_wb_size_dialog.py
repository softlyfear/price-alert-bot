"""Selected-size callback and dialog preserve the tracking identity."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.bot import texts
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import AddRefCb
from app.bot.keyboards import AddWbSizeCb
from app.bot.states import AddProduct
from app.domain.links import MAX_ARTICLE
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.schemas.marketplace import MarketplaceProductData
from app.schemas.marketplace import WbSizeData
from tests.unit.conftest import sent_message_id
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _Dialog


def test_size_callback_maximum_and_old_format() -> None:
    packed = AddWbSizeCb(article=MAX_ARTICLE, wb_size_id=MAX_ARTICLE).pack()
    assert len(packed.encode("utf-8")) <= 64
    assert AddWbSizeCb.unpack(packed).wb_size_id == MAX_ARTICLE
    assert AddRefCb(marketplace=Marketplace.wb, article=123).pack() == "addref:wb:123"


@pytest.mark.parametrize(
    "article,size", [(0, 1), (1, 0), (MAX_ARTICLE + 1, 1), (1, MAX_ARTICLE + 1)]
)
def test_invalid_size_callback_is_rejected(article: int, size: int) -> None:
    with pytest.raises(ValidationError):
        AddWbSizeCb(article=article, wb_size_id=size)


@pytest.mark.asyncio
async def test_selected_callback_through_confirmation_preserves_size(
    card_dialog: _Dialog, card_service: MagicMock
) -> None:
    card_service.find_tracked_product.return_value = None
    card_service.preview.return_value = MarketplaceProductData(
        name="Item",
        price=599200,
        wb_sizes={42: WbSizeData(name="54-188", price=599200)},
    )
    card_service.add_tracking.return_value.direction = AlertDirection.below
    await card_dialog.press("addsz:123:42")
    card_service.preview.assert_awaited_once_with(
        _USER, Marketplace.wb, 123, wb_size_id=42
    )
    assert card_dialog.edits[-1].text is not None
    assert "54-188" in card_dialog.edits[-1].text
    assert (await card_dialog.data())["wb_size_id"] == 42
    await card_dialog.say("5992")
    assert card_dialog.sent[-1].text == texts.ADD_TARGET_EQUALS_TEXT
    await card_dialog.say("5500")
    assert "54-188" in card_dialog.sent[-1].text
    await card_dialog.press(CB_ADD_CONFIRM, message_id=sent_message_id(1))
    assert card_service.add_tracking.await_args.args == (
        _USER,
        Marketplace.wb,
        123,
        "Item",
        599200,
        550000,
    )
    assert card_service.add_tracking.await_args.kwargs["wb_size_id"] == 42
    assert card_service.add_tracking.await_args.kwargs["wb_size_name"] == "54-188"
    assert "54-188" in card_dialog.sent[-1].text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, -1, "42", True, MAX_ARTICLE + 1])
async def test_invalid_present_size_draft_is_stale(
    card_dialog: _Dialog, card_service: MagicMock, value: Any
) -> None:
    await card_dialog.set_state(
        AddProduct.waiting_target_price.state,
        {
            "product_name": "Item",
            "current_price": 1000,
            "wb_size_id": value,
            "wb_size_name": None,
        },
    )
    await card_dialog.say("5")
    assert card_dialog.sent[-1].text == texts.ADD_STALE_TEXT
    card_service.add_tracking.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", ["addsz:0:1", "addsz:1:0", "addsz:1:x", "addsz:1:2:3"]
)
async def test_forged_size_callback_is_stale(
    card_dialog: _Dialog, card_service: MagicMock, payload: str
) -> None:
    await card_dialog.press(payload)
    assert [(a.text, a.show_alert) for a in card_dialog.callback_answers] == [
        (texts.ADD_STALE_ALERT_TEXT, True)
    ]
    card_service.preview.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size,name,marketplace",
    [(0, "orphan", "wb"), (42, 7, "wb"), (42, "x" * 151, "wb"), (42, "42", "ozon")],
)
async def test_inconsistent_size_draft_is_stale(
    card_dialog: _Dialog,
    card_service: MagicMock,
    size: int,
    name: Any,
    marketplace: str,
) -> None:
    await card_dialog.set_state(
        AddProduct.waiting_target_price.state,
        {
            "product_name": "Item",
            "current_price": 1000,
            "marketplace": marketplace,
            "wb_size_id": size,
            "wb_size_name": name,
        },
    )
    await card_dialog.say("5")
    assert card_dialog.sent[-1].text == texts.ADD_STALE_TEXT
    card_service.add_tracking.assert_not_called()


def test_size_label_in_each_text_screen_and_numeric_fallback() -> None:
    screens = [
        texts.product_found_text(Marketplace.wb, "Item", 1000, wb_size_id=42),
        texts.confirm_text("Item", 1000, 500, AlertDirection.below, wb_size_id=42),
        texts.added_text("Item", 500, AlertDirection.below, wb_size_id=42),
        texts.product_card_text("Item", Marketplace.wb, "url", 1000, [], wb_size_id=42),
        texts.edit_prompt_text("Item", 1000, AlertDirection.below, 500, wb_size_id=42),
    ]
    assert all("Размер: 42" in screen for screen in screens)
    assert texts.size_suffix(42, "54-188") == " · Размер: 54-188"
    assert texts.size_suffix() == ""
