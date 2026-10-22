"""Unreachable `assert_never` arms of the `ProductQuery` branching (PAB-114 AC 7).

Every branching over the link variants ends in `case _: assert_never(...)`, so a
new variant fails `mypy`, not production. The arm cannot be reached with a real
value; each test feeds an object of an unknown type and expects the loud
failure, which the dispatcher turns into the generic error reply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import app.handlers.add_product as add_product_module
import app.handlers.fallback as fallback_module
from app.bot import texts
from app.bot.states import AddProduct
from app.domain.links import OzonShortLink
from app.domain.links import ProductQuery
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.tracking import TrackingService
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.test_short_link import _SHARE_TEXT
from tests.unit.handlers.test_short_link import _ShortTracking

if TYPE_CHECKING:
    from loguru import Record

_UNKNOWN = cast(ProductQuery, object())


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _ShortTracking:
    tracking = _ShortTracking()
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_: tracking)
    return tracking


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(_Faults())))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, _Faults())


def _error_logged(records: list[Record]) -> bool:
    return any(
        isinstance(r["exception"].value, AssertionError)
        for r in records
        if r["exception"] is not None
    )


@pytest.mark.asyncio
async def test_unknown_expansion_outcome_ends_in_the_generic_error_reply(
    dialog: _Dialog, stub: _ShortTracking, log_records: list[Record]
) -> None:
    stub.resolve_outcome = object()
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert texts.UNEXPECTED_ERROR_TEXT in [m.text for m in dialog.sent]
    assert stub.preview_calls == []
    assert _error_logged(log_records)


@pytest.mark.asyncio
async def test_unknown_variant_in_the_link_step_ends_in_the_generic_error_reply(
    dialog: _Dialog,
    stub: _ShortTracking,
    monkeypatch: pytest.MonkeyPatch,
    log_records: list[Record],
) -> None:
    monkeypatch.setattr(add_product_module, "extract_product_ref", lambda _t: _UNKNOWN)
    await dialog.command("add")

    await dialog.say("anything")

    assert texts.UNEXPECTED_ERROR_TEXT in [m.text for m in dialog.sent]
    assert stub.find_product_calls == []
    assert _error_logged(log_records)


@pytest.mark.asyncio
async def test_unknown_variant_in_free_text_gets_no_offer(
    dialog: _Dialog, monkeypatch: pytest.MonkeyPatch, log_records: list[Record]
) -> None:
    monkeypatch.setattr(fallback_module, "extract_product_ref", lambda _t: _UNKNOWN)

    await dialog.say("anything")

    assert [m.text for m in dialog.sent] == [texts.UNEXPECTED_ERROR_TEXT]
    assert _error_logged(log_records)


@pytest.mark.asyncio
async def test_preview_and_check_refuse_an_unknown_variant() -> None:
    tracking = cast(TrackingService, _ShortTracking())
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=_USER))
    zone = cast(Any, None)

    with pytest.raises(AssertionError):
        await add_product_module._preview(tracking, _USER, cast(Any, _UNKNOWN))
    with pytest.raises(AssertionError):
        await add_product_module._check_and_deliver(
            zone, 1, None, _UNKNOWN, _USER, state, tracking, zone
        )


@pytest.mark.asyncio
async def test_check_refuses_an_unknown_target_after_a_failed_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`target` is always the ref itself or an expansion; a stranger is loud."""
    tracking = cast(TrackingService, _ShortTracking())
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=_USER))
    await state.set_state(AddProduct.waiting_link)
    await state.update_data({"link_message_id": 1})

    async def expand(*_a: Any) -> Any:
        return object()

    async def preview(*_a: Any) -> Any:
        failure = MarketplaceFetchFailure(reason=FetchFailureReason.blocked, detail="d")
        return Marketplace.ozon, failure

    monkeypatch.setattr(add_product_module, "_expand_short_link", expand)
    monkeypatch.setattr(add_product_module, "_preview", preview)

    with pytest.raises(AssertionError):
        await add_product_module._check_and_deliver(
            cast(Any, None),
            1,
            None,
            OzonShortLink("abc"),
            _USER,
            state,
            tracking,
            cast(Any, None),
        )


def test_short_link_failure_text_refuses_an_unknown_reason() -> None:
    with pytest.raises(AssertionError):
        texts.short_link_failure_text(cast(FetchFailureReason, "unknown"))
