"""Ozon short link `ozon.ru/t/<code>` in the /add dialog and free text.

PAB-114, PROJECT.md 2.15 R22 items 4 and 6. Everything goes through
``Dispatcher.feed_update``; the replaced boundary is ``TrackingService``
(``_ShortTracking``: ``resolve_short_link`` and the tracked-product lookup are
scripted and recorded). Texts are written out by hand where the wording is the
point of the test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import AddShortLinkCb
from app.bot.states import AddProduct
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.links import ProductRef
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.tracking import ShortLinkRefusal
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.conftest import sent_message_id
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _CONFIRMING_DATA
from tests.unit.handlers.add_dialog_support import _LINK_A
from tests.unit.handlers.add_dialog_support import _NAME
from tests.unit.handlers.add_dialog_support import _PRICE
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _WAIT
from tests.unit.handlers.add_dialog_support import _assert_only_dropped_edit
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.add_dialog_support import _shown_texts
from tests.unit.handlers.add_dialog_support import _TrackingStub
from tests.unit.handlers.card_dialog_support import CARD_TEXT
from tests.unit.handlers.card_dialog_support import card

if TYPE_CHECKING:
    from loguru import Record

_CODE = "7GeNMx2"
_ARTICLE = 2622563074
_SHARE_TEXT = f"Лонгслив\nhttps://ozon.ru/t/{_CODE}"
_PAYLOAD = f"addsl:{_CODE}"
_REF = ProductRef(Marketplace.ozon, _ARTICLE)
_ALREADY = "Товар уже отслеживается"


class _ShortTracking(_TrackingStub):
    """Scripted ``resolve_short_link`` and ``find_tracked_product`` with a record."""

    def __init__(self) -> None:
        super().__init__()
        self.resolve_calls: list[tuple[int, str]] = []
        self.resolve_outcome: Any = _REF
        self.resolve_error: Exception | None = None
        self.hold_first: asyncio.Event | None = None
        self.first_entered = asyncio.Event()
        self.after_resolve: Callable[[], Awaitable[None]] | None = None
        self.find_product_calls: list[tuple[int, Marketplace, int]] = []
        self.found_product: Any = None
        self.after_find: Callable[[], Awaitable[None]] | None = None

    async def resolve_short_link(self, code: str) -> Any:
        # Called without the user id: the service has no per-user state.
        index = len(self.resolve_calls)
        self.resolve_calls.append((_USER, code))
        if index == 0 and self.hold_first is not None:
            self.first_entered.set()
            await self.hold_first.wait()
        if self.after_resolve is not None:
            await self.after_resolve()
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.resolve_outcome

    async def find_tracked_product(
        self, tg_user_id: int, marketplace: Marketplace, article: int
    ) -> Any:
        self.find_product_calls.append((tg_user_id, marketplace, article))
        if self.after_find is not None:
            await self.after_find()
        return self.found_product


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _ShortTracking:
    tracking = _ShortTracking()
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_: tracking)
    return tracking


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


def _failure(reason: FetchFailureReason) -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(reason=reason, detail="SECRET-DETAIL-MARKER")


def _callbacks(request: SendMessage | EditMessageText) -> list[str | None]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _assert_no_service_use(stub: _ShortTracking) -> None:
    assert stub.resolve_calls == []
    assert stub.find_product_calls == []
    assert stub.find_article_calls == []
    assert stub.preview_calls == []
    assert stub.preview_article_calls == []
    assert stub.add_calls == []


# --- /add, link step ---------------------------------------------------------


@pytest.mark.asyncio
async def test_short_link_is_expanded_previewed_and_confirmed_as_an_ozon_product(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert stub.resolve_calls == [(_USER, _CODE)]
    assert stub.find_product_calls == [(_USER, Marketplace.ozon, _ARTICLE)]
    assert stub.preview_calls == [(_USER, Marketplace.ozon, _ARTICLE)]
    assert _USER != _CHAT
    assert [m.text for m in dialog.sent][1] == texts.ADD_CHECKING_TEXT
    assert dialog.edits[-1].text.startswith(f"Нашёл товар на Ozon:\n{_NAME}\n")
    data = await dialog.data()
    assert (data["marketplace"], data["article"]) == ("ozon", _ARTICLE)
    assert await dialog.state() == AddProduct.waiting_target_price.state

    await dialog.say("1500")
    confirm_id = (await dialog.data())["confirm_message_id"]
    await dialog.press(CB_ADD_CONFIRM, message_id=confirm_id)

    assert stub.add_calls == [
        (_USER, Marketplace.ozon, _ARTICLE, _NAME, _PRICE, 150_000)
    ]
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_already_tracked_short_link_shows_the_card_without_a_preview(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    stub.found_product = card()
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert stub.preview_calls == []
    shown = dialog.edits[-1]
    assert shown.text == f"{_ALREADY}\n\n{CARD_TEXT}"
    assert shown.parse_mode is None
    assert shown.message_id == sent_message_id(1)
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(FetchFailureReason))
async def test_expansion_failure_shows_its_text_and_keeps_the_link_step(
    dialog: _Dialog, stub: _ShortTracking, reason: FetchFailureReason
) -> None:
    stub.resolve_outcome = _failure(reason)
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    shown = dialog.edits[-1]
    assert shown.text == texts.short_link_failure_text(reason)
    assert "SECRET-DETAIL-MARKER" not in shown.text
    assert _callbacks(shown) == [CB_ADD_CANCEL]
    assert stub.preview_calls == []
    assert stub.find_product_calls == []
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_location_that_is_not_a_product_asks_for_the_full_link(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    stub.resolve_outcome = ShortLinkRefusal.not_a_product
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    shown = dialog.edits[-1]
    assert shown.text == texts.SHORT_LINK_UNRESOLVED_TEXT
    assert "полную ссылку" in shown.text
    assert _callbacks(shown) == [CB_ADD_CANCEL]
    assert stub.preview_calls == []
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_unsupported_marketplace_on_expansion_keeps_the_link_step(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    stub.resolve_error = MarketplaceNotSupportedError("ozon")
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert dialog.edits[-1].text == texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT
    assert stub.preview_calls == []
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_exhausted_limit_costs_one_expansion_and_shows_the_limit_text(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    stub.preview_error = ProductLimitExceededError(37)
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert len(stub.resolve_calls) == 1
    assert len(stub.preview_calls) == 1
    assert dialog.edits[-1].text == texts.limit_exceeded_text(37)
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_preview_failure_after_expansion_uses_the_link_texts(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    stub.preview_result = _failure(FetchFailureReason.blocked)
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert (
        dialog.edits[-1].text
        == texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.blocked]
    )
    assert await dialog.state() == AddProduct.waiting_link.state


# --- races ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_link_during_expansion_wins_and_the_first_notice_is_dropped(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    stub.hold_first = asyncio.Event()
    await dialog.command("add")
    first = asyncio.create_task(dialog.say(_SHARE_TEXT, message_id=101))
    async with asyncio.timeout(_WAIT):
        await stub.first_entered.wait()
    notice_id = sent_message_id(
        next(i for i, m in enumerate(dialog.sent) if m.text == texts.ADD_CHECKING_TEXT)
    )

    await dialog.say(_LINK_A, message_id=102)
    base = len(dialog.requests)
    stub.hold_first.set()
    async with asyncio.timeout(_WAIT):
        await first

    _assert_only_dropped_edit(dialog, base, notice_id)
    assert stub.preview_calls == [(_USER, Marketplace.wb, 11111111)]
    # Only the second (full) link looked a product up: the first never got there.
    assert stub.find_product_calls == [(_USER, Marketplace.wb, 11111111)]
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert (await dialog.data())["link_message_id"] == 102


@pytest.mark.asyncio
async def test_step_overtaken_during_expansion_shows_no_outcome(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    async def move_on() -> None:
        await dialog.set_state(None)

    stub.after_resolve = move_on
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert dialog.edits[-1].text == texts.ADD_CHECK_DROPPED_TEXT
    assert stub.find_product_calls == []
    assert stub.preview_calls == []
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_step_overtaken_during_the_tracked_lookup_shows_no_card_and_no_preview(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    async def move_on() -> None:
        await dialog.set_state(None)

    stub.found_product = card()
    stub.after_find = move_on
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert stub.preview_calls == []
    assert CARD_TEXT not in _shown_texts(dialog)
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_step_overtaken_during_the_tracked_lookup_retitles_the_notice(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    """The "checking" notice must not stay on the screen forever (AC 4)."""

    async def move_on() -> None:
        await dialog.set_state(None)

    stub.found_product = card()
    stub.after_find = move_on
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert [e.text for e in dialog.edits] == [texts.ADD_CHECK_DROPPED_TEXT]


@pytest.mark.asyncio
async def test_unsupported_marketplace_after_the_step_was_overtaken_shows_only_the_drop(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    async def move_on() -> None:
        await dialog.set_state(None)

    stub.after_resolve = move_on
    stub.resolve_error = MarketplaceNotSupportedError("ozon")
    await dialog.command("add")

    await dialog.say(_SHARE_TEXT)

    assert [e.text for e in dialog.edits] == [texts.ADD_CHECK_DROPPED_TEXT]
    assert texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT not in _shown_texts(dialog)


# --- regressions of the link step ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "https://ozon.ru/t/a-b",
        "https://ozon.ru/t/" + "a" * 33,
        "https://ozon.ru/t/AbCd/extra",
        "https://ozon.ru.evil.com/t/AbCd",
    ],
)
async def test_malformed_short_link_gets_the_format_hint_and_reaches_no_service(
    dialog: _Dialog, stub: _ShortTracking, bad: str
) -> None:
    await dialog.command("add")

    await dialog.say(bad)

    assert dialog.sent[-1].text == texts.ADD_LINK_FORMAT_HINT_TEXT
    assert await dialog.state() == AddProduct.waiting_link.state
    _assert_no_service_use(stub)


@pytest.mark.asyncio
async def test_short_and_full_link_together_get_the_one_link_request(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    await dialog.command("add")

    await dialog.say(f"https://ozon.ru/t/{_CODE} {_LINK_A}")

    assert dialog.sent[-1].text == texts.MULTIPLE_PRODUCT_REFS_TEXT
    _assert_no_service_use(stub)


@pytest.mark.asyncio
async def test_cancel_after_a_short_link_card_drops_the_draft(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    await dialog.command("add")
    await dialog.say(_SHARE_TEXT)

    await dialog.command("cancel")

    assert await dialog.state() is None
    assert await dialog.data() == {}
    assert stub.add_calls == []


# --- free text outside a dialog -------------------------------------------------


@pytest.mark.asyncio
async def test_short_link_in_free_text_gets_an_offer_and_touches_no_service(
    dialog: _Dialog, stub: _ShortTracking, log_records: list[Record]
) -> None:
    await dialog.say(_SHARE_TEXT)

    assert len(dialog.requests) == 1
    offer = dialog.sent[0]
    assert offer.text == "Добавить этот товар? Ozon, короткая ссылка."
    assert offer.parse_mode is None
    assert _callbacks(offer) == [_PAYLOAD, "menu:start"]
    assert await dialog.state() is None
    _assert_no_service_use(stub)
    assert [
        r for r in log_records if _CODE in str(r["message"]) + str(r["extra"])
    ] == []


@pytest.mark.asyncio
async def test_pressing_the_offer_runs_the_same_path_in_place(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    await dialog.press(_PAYLOAD)

    assert dialog.sent == []
    checking, shown = dialog.edits
    assert checking.text == texts.ADD_CHECKING_TEXT
    assert shown.text.startswith(f"Нашёл товар на Ozon:\n{_NAME}\n")
    assert (shown.chat_id, shown.message_id) == (_CHAT, _CARD_ID)
    assert stub.resolve_calls == [(_USER, _CODE)]
    assert stub.preview_calls == [(_USER, Marketplace.ozon, _ARTICLE)]
    data = await dialog.data()
    assert (data["marketplace"], data["article"]) == ("ozon", _ARTICLE)
    assert await dialog.state() == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
async def test_pressing_the_offer_for_a_failed_expansion_shows_the_refusal(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    stub.resolve_outcome = _failure(FetchFailureReason.not_found)

    await dialog.press(_PAYLOAD)

    assert dialog.edits[-1].text == texts.short_link_failure_text(
        FetchFailureReason.not_found
    )
    assert stub.preview_calls == []
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_offer_button_without_a_message_pops_up_the_stale_alert(
    dialog: _Dialog, stub: _ShortTracking
) -> None:
    await dialog.press(_PAYLOAD, with_message=False)

    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.ADD_STALE_ALERT_TEXT, True)
    ]
    _assert_no_service_use(stub)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    ["addsl:a-b", "addsl:" + "a" * 33, "addsl:", "addsl", "addsl:a:b", "addsl:код"],
)
async def test_forged_short_link_payload_pops_up_stale_alert_without_service_use(
    dialog: _Dialog, stub: _ShortTracking, payload: str
) -> None:
    await dialog.set_state(AddProduct.confirming.state, dict(_CONFIRMING_DATA))

    await dialog.press(payload)

    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.ADD_STALE_ALERT_TEXT, True)
    ]
    assert [type(r) for r in dialog.requests] == [AnswerCallbackQuery]
    _assert_no_service_use(stub)
    assert await dialog.state() == AddProduct.confirming.state
    assert await dialog.data() == _CONFIRMING_DATA


def test_callback_data_of_a_32_char_code_fits_the_telegram_limit() -> None:
    packed = AddShortLinkCb(code="a" * 32).pack()

    assert packed == "addsl:" + "a" * 32
    assert len(packed.encode()) <= 64


# --- texts ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", [FetchFailureReason.blocked, FetchFailureReason.transport_error]
)
def test_blocked_and_transport_texts_name_ozon(reason: FetchFailureReason) -> None:
    assert "Ozon" in texts.short_link_failure_text(reason)


def test_short_link_refusals_are_distinguishable_by_cause() -> None:
    by_cause = {
        texts.short_link_failure_text(FetchFailureReason.bad_payload),
        texts.short_link_failure_text(FetchFailureReason.not_found),
        texts.short_link_failure_text(FetchFailureReason.blocked),
        texts.short_link_failure_text(FetchFailureReason.transport_error),
    }

    assert len(by_cause) == 4
    assert texts.short_link_failure_text(FetchFailureReason.out_of_stock) == (
        texts.SHORT_LINK_UNRESOLVED_TEXT
    )
    assert "устарела" in texts.short_link_failure_text(FetchFailureReason.not_found)
