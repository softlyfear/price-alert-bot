"""Fixtures of the PAB-096 handler tests; ``TrackingService`` is the boundary."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram import Dispatcher

import app.handlers.add_product as add_product_module
import app.handlers.list_products as list_products_module
import app.handlers.product_card as product_card_module
from app.services.tracking import TrackingService
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.card_dialog_support import SCREEN_NOW
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import page_of


class _ScreenClock:
    """Stand-in for the ``datetime`` name of the screen modules: only ``now``."""

    @staticmethod
    def now(tz: object = None) -> datetime:
        return SCREEN_NOW


@pytest.fixture(autouse=True)
def frozen_screen_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze the "now" of the card and list screens (the price time label)."""
    monkeypatch.setattr(product_card_module, "datetime", _ScreenClock)
    monkeypatch.setattr(list_products_module, "datetime", _ScreenClock)
    return SCREEN_NOW


@pytest.fixture
def card_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """``TrackingService`` double: the product is tracked, nothing else is set."""
    mock = MagicMock(spec=TrackingService)
    mock.find_tracked_product.return_value = card()
    mock.get_product_card.return_value = card()
    mock.list_page.return_value = page_of([])
    mock.list_page_of.return_value = 1
    mock.product_limit.return_value = 50
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: mock)
    return mock


@pytest.fixture
def card_dialog(real_dispatcher: Dispatcher, card_service: MagicMock) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)
