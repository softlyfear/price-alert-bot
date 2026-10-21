"""Fixtures of the PAB-096 handler tests; ``TrackingService`` is the boundary."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram import Dispatcher

import app.handlers.add_product as add_product_module
from app.services.tracking import TrackingService
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.card_dialog_support import card


@pytest.fixture
def card_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """``TrackingService`` double: the product is tracked, nothing else is set."""
    mock = MagicMock(spec=TrackingService)
    mock.find_tracked_product.return_value = card()
    mock.get_product_card.return_value = card()
    mock.list_products.return_value = []
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: mock)
    return mock


@pytest.fixture
def card_dialog(real_dispatcher: Dispatcher, card_service: MagicMock) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)
