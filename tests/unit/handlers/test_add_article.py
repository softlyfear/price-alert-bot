"""Bare article (digits only) in the /add dialog, free text and the button.

PAB-113, PROJECT.md 2.15 R21. Everything goes through ``Dispatcher.feed_update``;
the replaced boundary is ``TrackingService`` (``_TrackingStub``). Expected
texts are written out by hand where the wording is the point of the test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import AddArticleCb
from app.bot.states import AddProduct
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.links import MAX_ARTICLE
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.services.tracking import ArticlePreview
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _CONFIRMING_DATA
from tests.unit.handlers.add_dialog_support import _NAME
from tests.unit.handlers.add_dialog_support import _PRICE
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.add_dialog_support import _shown_texts
from tests.unit.handlers.add_dialog_support import _TrackingStub
from tests.unit.handlers.card_dialog_support import CARD_TEXT
from tests.unit.handlers.card_dialog_support import card
from tests.unit.handlers.card_dialog_support import rows

if TYPE_CHECKING:
    from loguru import Record

_OZON_NUMBER = 5_764_570_826  # a real Ozon article (customer's screenshot)
_NUMBER_TEXT = str(_OZON_NUMBER)
_PAYLOAD = f"addart:{_OZON_NUMBER}"
_ALREADY = "Товар уже отслеживается"
_NO_SERVICE_CALLS: list[tuple[int, int]] = []


def _failure(reason: FetchFailureReason) -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(reason=reason, detail="SECRET-DETAIL-MARKER")


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _TrackingStub:
    tracking = _TrackingStub()
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_: tracking)
    return tracking


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


def _assert_no_service_use(stub: _TrackingStub) -> None:
    assert stub.find_article_calls == _NO_SERVICE_CALLS
    assert stub.preview_article_calls == _NO_SERVICE_CALLS
    assert stub.preview_calls == []
    assert stub.add_calls == []


def _markup_rows(request: SendMessage) -> list[list[tuple[str, str | None]]]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


# --- /add, link step ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tracked_bare_article_shows_the_card_without_checking_or_preview(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.found_article = card()
    await dialog.command("add")
    base = len(dialog.requests)

    await dialog.say(_NUMBER_TEXT)

    step = dialog.requests[base:]
    assert [type(r) for r in step] == [SendMessage]
    shown = step[0]
    assert isinstance(shown, SendMessage)
    assert shown.text == f"{_ALREADY}\n\n{CARD_TEXT}"
    assert shown.parse_mode is None
    assert texts.ADD_CHECKING_TEXT not in _shown_texts(dialog)
    assert stub.find_article_calls == [(_USER, _OZON_NUMBER)]
    assert stub.preview_article_calls == []
    assert stub.preview_calls == []
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_untracked_bare_article_says_checking_then_previews_for_the_sender(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")

    await dialog.say(_NUMBER_TEXT)

    assert stub.find_article_calls == [(_USER, _OZON_NUMBER)]
    assert stub.preview_article_calls == [(_USER, _OZON_NUMBER)]
    assert stub.preview_calls == []
    assert _USER != _CHAT
    assert [m.text for m in dialog.sent][1] == texts.ADD_CHECKING_TEXT


@pytest.mark.asyncio
async def test_wb_data_gives_a_wildberries_card_and_the_draft_keeps_wb(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.article_preview = ArticlePreview(
        Marketplace.wb, MarketplaceProductData(name=_NAME, price=_PRICE)
    )
    await dialog.command("add")

    await dialog.say("12345678")

    shown = dialog.edits[-1]
    assert shown.text.startswith(f"Нашёл товар на Wildberries:\n{_NAME}\n")
    assert shown.parse_mode is None
    data = await dialog.data()
    assert (data["marketplace"], data["article"]) == ("wb", 12345678)
    assert await dialog.state() == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
async def test_ozon_data_gives_an_ozon_card_and_the_draft_keeps_ozon(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.article_preview = ArticlePreview(
        Marketplace.ozon, MarketplaceProductData(name=_NAME, price=_PRICE)
    )
    await dialog.command("add")

    await dialog.say(_NUMBER_TEXT)

    assert dialog.edits[-1].text.startswith(f"Нашёл товар на Ozon:\n{_NAME}\n")
    data = await dialog.data()
    assert (data["marketplace"], data["article"]) == ("ozon", _OZON_NUMBER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marketplace", "expected"),
    [(Marketplace.wb, Marketplace.wb), (Marketplace.ozon, Marketplace.ozon)],
)
async def test_confirmation_adds_tracking_with_the_marketplace_of_the_preview(
    dialog: _Dialog,
    stub: _TrackingStub,
    marketplace: Marketplace,
    expected: Marketplace,
) -> None:
    stub.article_preview = ArticlePreview(
        marketplace, MarketplaceProductData(name=_NAME, price=_PRICE)
    )
    await dialog.command("add")
    await dialog.say(_NUMBER_TEXT)
    await dialog.say("1500")
    confirm_id = (await dialog.data())["confirm_message_id"]

    await dialog.press(CB_ADD_CONFIRM, message_id=confirm_id)

    assert stub.add_calls == [(_USER, expected, _OZON_NUMBER, _NAME, _PRICE, 150_000)]
    assert await dialog.state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marketplace", "reason"),
    [
        (marketplace, reason)
        for marketplace in Marketplace
        for reason in FetchFailureReason
    ],
)
async def test_article_failure_text_names_where_it_was_looked_for_and_keeps_the_step(
    dialog: _Dialog,
    stub: _TrackingStub,
    marketplace: Marketplace,
    reason: FetchFailureReason,
) -> None:
    stub.article_preview = ArticlePreview(marketplace, _failure(reason))
    await dialog.command("add")

    await dialog.say(_NUMBER_TEXT)

    shown = dialog.edits[-1]
    expected = (
        texts.ARTICLE_WB_FAILURE_TEXTS[reason]
        if marketplace is Marketplace.wb
        else texts.ARTICLE_OZON_FAILURE_TEXTS[reason]
    )
    assert shown.text == expected
    assert "SECRET-DETAIL-MARKER" not in shown.text
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.parametrize(
    ("reason", "phrase"),
    [
        (FetchFailureReason.not_found, "ни на Wildberries, ни на Ozon"),
        (FetchFailureReason.bad_payload, "Ozon не вернул данные о товаре"),
        (FetchFailureReason.blocked, "проверить Ozon сейчас не удалось"),
        (FetchFailureReason.transport_error, "проверить Ozon сейчас не удалось"),
    ],
)
def test_ozon_refusal_texts_after_a_wb_miss_say_what_is_known_and_what_is_not(
    reason: FetchFailureReason, phrase: str
) -> None:
    text = texts.article_failure_text(Marketplace.ozon, reason)

    assert phrase in text
    assert "ошибка Ozon" not in text and "ошибки Ozon" not in text


def test_ozon_bad_payload_text_does_not_claim_the_product_is_absent() -> None:
    text = texts.article_failure_text(Marketplace.ozon, FetchFailureReason.bad_payload)

    assert "ни на Wildberries" not in text
    assert "ссылк" in text


def test_the_four_ozon_refusals_are_distinguishable() -> None:
    four = {
        texts.article_failure_text(Marketplace.ozon, reason)
        for reason in (
            FetchFailureReason.not_found,
            FetchFailureReason.bad_payload,
            FetchFailureReason.blocked,
            FetchFailureReason.transport_error,
        )
    }

    assert len(four) == 4


def test_wb_failure_texts_name_wildberries() -> None:
    for reason in FetchFailureReason:
        assert "Wildberries" in texts.article_failure_text(Marketplace.wb, reason)


@pytest.mark.asyncio
async def test_limit_on_the_article_path_shows_the_limit_and_clears_the_dialog(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_error = ProductLimitExceededError(37)
    await dialog.command("add")

    await dialog.say(_NUMBER_TEXT)

    assert dialog.edits[-1].text == texts.limit_exceeded_text(37)
    assert await dialog.state() is None
    assert await dialog.data() == {}


@pytest.mark.asyncio
async def test_unsupported_marketplace_on_the_article_path_keeps_the_link_step(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_error = MarketplaceNotSupportedError("ozon")
    await dialog.command("add")

    await dialog.say(_NUMBER_TEXT)

    assert dialog.edits[-1].text == texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0", "9223372036854775808", "١٢٣", "12 34"])
async def test_wrong_number_format_keeps_the_link_step_and_reaches_no_service(
    dialog: _Dialog, stub: _TrackingStub, bad: str
) -> None:
    await dialog.command("add")

    await dialog.say(bad)

    assert dialog.sent[-1].text == texts.ADD_LINK_FORMAT_HINT_TEXT
    assert await dialog.state() == AddProduct.waiting_link.state
    _assert_no_service_use(stub)


@pytest.mark.asyncio
async def test_invalid_price_after_an_article_card_keeps_the_draft(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")
    await dialog.say(_NUMBER_TEXT)

    await dialog.say("abc")

    assert dialog.sent[-1].text == texts.ADD_PRICE_INVALID_TEXT
    assert await dialog.state() == AddProduct.waiting_target_price.state
    assert (await dialog.data())["article"] == _OZON_NUMBER


@pytest.mark.asyncio
async def test_cancel_after_an_article_card_drops_the_draft(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.command("add")
    await dialog.say(_NUMBER_TEXT)

    await dialog.command("cancel")

    assert await dialog.state() is None
    assert await dialog.data() == {}
    assert stub.add_calls == []


@pytest.mark.asyncio
async def test_step_overtaken_during_the_article_preview_shows_no_outcome(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    class _Moving(_TrackingStub):
        async def preview_article(
            self, tg_user_id: int, article: int
        ) -> ArticlePreview:
            await dialog.set_state(None)
            return await super().preview_article(tg_user_id, article)  # type: ignore[no-any-return]

    moving = _Moving()
    moving.article_preview = ArticlePreview(
        Marketplace.ozon, MarketplaceProductData(name=_NAME, price=_PRICE)
    )
    stub.preview_article = moving.preview_article  # type: ignore[method-assign]
    await dialog.command("add")

    await dialog.say(_NUMBER_TEXT)

    assert dialog.edits[-1].text == texts.ADD_CHECK_DROPPED_TEXT
    assert _NAME not in _shown_texts(dialog)
    assert await dialog.state() is None
    assert await dialog.data() == {}


# --- free text outside a dialog ------------------------------------------------


@pytest.mark.asyncio
async def test_bare_digits_in_free_text_get_an_offer_without_a_single_marketplace(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.say(_NUMBER_TEXT)

    assert len(dialog.requests) == 1
    offer = dialog.sent[0]
    assert offer.text == (
        f"Добавить этот товар? Артикул {_OZON_NUMBER}: поищу его сначала на "
        "Wildberries, затем на Ozon."
    )
    assert offer.parse_mode is None
    assert _markup_rows(offer) == [
        [("➕ Добавить этот товар", _PAYLOAD)],
        [("Главное меню", "menu:start")],
    ]
    assert await dialog.state() is None
    _assert_no_service_use(stub)


@pytest.mark.asyncio
async def test_bare_digits_offer_is_not_written_to_the_journal(
    dialog: _Dialog, stub: _TrackingStub, log_records: list[Record]
) -> None:
    await dialog.say(_NUMBER_TEXT)

    assert len(dialog.sent) == 1  # positive control: the message was handled
    assert [
        r for r in log_records if _NUMBER_TEXT in str(r["message"]) + str(r["extra"])
    ] == []


@pytest.mark.parametrize("article", [1, MAX_ARTICLE])
def test_article_callback_data_fits_the_telegram_limit(article: int) -> None:
    packed = AddArticleCb(article=article).pack()

    assert packed == f"addart:{article}"
    assert len(packed.encode()) <= 64


# --- the "add this product" button of the offer ----------------------------------


@pytest.mark.asyncio
async def test_button_on_a_tracked_article_edits_the_message_into_the_card(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.found_article = card()

    await dialog.press(_PAYLOAD)

    assert [type(r) for r in dialog.requests] == [EditMessageText, AnswerCallbackQuery]
    edit = dialog.edits[0]
    assert (edit.chat_id, edit.message_id) == (_CHAT, _CARD_ID)
    assert edit.text == f"{_ALREADY}\n\n{CARD_TEXT}"
    assert rows(edit)[0][0][1] == "aedit:11:5"
    assert stub.find_article_calls == [(_USER, _OZON_NUMBER)]
    assert stub.preview_article_calls == []
    assert await dialog.state() is None


@pytest.mark.asyncio
async def test_button_on_an_untracked_article_shows_checking_then_the_card_in_place(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.article_preview = ArticlePreview(
        Marketplace.ozon, MarketplaceProductData(name=_NAME, price=_PRICE)
    )

    await dialog.press(_PAYLOAD)

    assert dialog.sent == []
    checking, shown = dialog.edits
    assert checking.text == texts.ADD_CHECKING_TEXT
    assert shown.text.startswith(f"Нашёл товар на Ozon:\n{_NAME}\n")
    assert (shown.chat_id, shown.message_id) == (_CHAT, _CARD_ID)
    assert stub.preview_article_calls == [(_USER, _OZON_NUMBER)]
    data = await dialog.data()
    assert (data["marketplace"], data["article"]) == ("ozon", _OZON_NUMBER)
    assert data["link_message_id"] == _CARD_ID
    assert await dialog.state() == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        _failure(FetchFailureReason.not_found),
        _failure(FetchFailureReason.bad_payload),
    ],
)
async def test_button_failure_uses_the_article_texts(
    dialog: _Dialog, stub: _TrackingStub, result: MarketplaceFetchResult
) -> None:
    stub.article_preview = ArticlePreview(Marketplace.ozon, result)

    await dialog.press(_PAYLOAD)

    assert isinstance(result, MarketplaceFetchFailure)
    assert dialog.edits[-1].text == texts.ARTICLE_OZON_FAILURE_TEXTS[result.reason]


@pytest.mark.asyncio
async def test_button_without_a_message_pops_up_the_stale_alert(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.press(_PAYLOAD, with_message=False)

    assert [(a.text, a.show_alert) for a in dialog.callback_answers] == [
        (texts.ADD_STALE_ALERT_TEXT, True)
    ]
    _assert_no_service_use(stub)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "addart:0",
        "addart:9223372036854775808",
        "addart:-5",
        "addart:abc",
        "addart:wb:5",
        "addart:5:7",
        "addart:",
        "addart",
    ],
)
async def test_forged_article_payload_pops_up_stale_alert_and_never_reaches_the_service(
    dialog: _Dialog, stub: _TrackingStub, payload: str
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
