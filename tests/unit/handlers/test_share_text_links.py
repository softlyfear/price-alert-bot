"""A "Share" text (title + link) in the /add dialog and in free text (PAB-107).

Goes through ``Dispatcher.feed_update``; the replaced boundary is
``TrackingService``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.types import InlineKeyboardMarkup

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.states import AddProduct
from app.models.enums import Marketplace
from app.schemas.marketplace import MarketplaceProductData
from app.services.tracking import TrackingService
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _keyboard_callbacks
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.add_dialog_support import _TrackingStub

_WB_LINK = "https://www.wildberries.ru/catalog/13511022/detail.aspx?size=41036292"
_SHARE = f"Брюки чиносы прямые без стрелок черные Westrenger\n{_WB_LINK}"
_OZON_LINK = "https://www.ozon.ru/product/nazvanie-tovara-123456789/?at=abc"
_TWO_PRODUCTS = f"{_WB_LINK}\n{_OZON_LINK}"


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _TrackingStub:
    tracking = _TrackingStub()

    def build(**kwargs: Any) -> _TrackingStub:
        tracking.init_kwargs.append(kwargs)
        return tracking

    monkeypatch.setattr(add_product_module, "TrackingService", build)
    return tracking


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(spec=TrackingService)
    mock.list_products.return_value = []
    mock.find_tracked_product.return_value = None
    mock.product_limit.return_value = 50
    mock.preview.return_value = MarketplaceProductData(name="Товар", price=100_00)
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: mock)
    return mock


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


def _without_fetch_time(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "price_fetched_at"}


# --- /add dialog ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_share_text_in_the_link_step_ends_like_the_bare_link(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")
    await dialog.say(_WB_LINK)
    bare = (
        dialog.outcome.text,
        _keyboard_callbacks(dialog.outcome),
        await dialog.state(),
        _without_fetch_time(await dialog.data()),
        list(stub.preview_calls),
    )
    await dialog.command("add")
    stub.preview_calls.clear()

    await dialog.say(_SHARE)

    shared = (
        dialog.outcome.text,
        _keyboard_callbacks(dialog.outcome),
        await dialog.state(),
        _without_fetch_time(await dialog.data()),
        list(stub.preview_calls),
    )
    assert shared == bare
    assert stub.preview_calls == [(_USER, Marketplace.wb, 13511022)]
    assert shared[2] == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
async def test_two_products_in_the_link_step_get_the_new_text_and_change_nothing(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")
    data_before = await dialog.data()

    await dialog.say(_TWO_PRODUCTS)

    assert dialog.last_text == texts.MULTIPLE_PRODUCT_REFS_TEXT
    assert dialog.last_text != texts.ADD_LINK_FORMAT_HINT_TEXT
    assert _keyboard_callbacks(dialog.outcome) == [CB_ADD_CANCEL]
    assert stub.preview_calls == []
    assert await dialog.state() == AddProduct.waiting_link.state
    assert await dialog.data() == data_before


@pytest.mark.asyncio
async def test_text_without_links_in_the_link_step_keeps_the_old_hint(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")

    await dialog.say("Купить 13511022 шт")

    assert dialog.last_text == texts.ADD_LINK_FORMAT_HINT_TEXT
    assert stub.preview_calls == []
    assert await dialog.state() == AddProduct.waiting_link.state


# --- free text -------------------------------------------------------------------


def _menu_callbacks(request: Any) -> list[str | None]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_share_text_outside_a_dialog_gets_the_offer_to_add(
    dialog: _Dialog, service: MagicMock
) -> None:
    await dialog.say(_SHARE)

    assert len(dialog.requests) == 1
    offer = dialog.sent[0]
    assert offer.text == "Добавить этот товар? Wildberries, артикул 13511022."
    assert _menu_callbacks(offer) == ["addref:wb:13511022", "menu:start"]
    assert service.mock_calls == []


@pytest.mark.asyncio
async def test_two_products_outside_a_dialog_get_the_new_text_with_the_main_menu(
    dialog: _Dialog, service: MagicMock
) -> None:
    await dialog.say(_TWO_PRODUCTS)

    assert len(dialog.requests) == 1
    answer = dialog.sent[0]
    assert answer.text == texts.MULTIPLE_PRODUCT_REFS_TEXT
    assert _menu_callbacks(answer) == [
        "menu:add",
        "menu:list",
        "menu:help",
    ]
    assert await dialog.state() is None
    assert service.mock_calls == []


@pytest.mark.asyncio
async def test_text_without_links_outside_a_dialog_gets_the_fallback_text(
    dialog: _Dialog, service: MagicMock
) -> None:
    await dialog.say("Цена 1990 ₽")

    assert [m.text for m in dialog.sent] == [texts.FALLBACK_TEXT]
    assert service.mock_calls == []
