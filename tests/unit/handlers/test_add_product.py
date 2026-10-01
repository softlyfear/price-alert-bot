"""Tests for the /add dialog, app.handlers.add_product (PAB-068 AC6).

Every scenario goes through ``Dispatcher.feed_update`` on the real
``create_dispatcher()`` output with a fake ``Bot`` session, so filter and
middleware wiring is exercised too. The boundary replaced is
``TrackingService`` (the handler's only collaborator): the stub records every
call and raises or returns what a scenario programs. Business rules live in
the service and are covered by ``tests/unit/services/test_tracking_service.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import httpx
import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.setup import create_dispatcher
from app.bot.states import AddProduct
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from app.services.client_factory import get_client
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_callback_update
from tests.unit.conftest import make_command_update
from tests.unit.conftest import make_message_update
from tests.unit.conftest import send_message_responder
from tests.unit.conftest import sent_message_id

if TYPE_CHECKING:
    from loguru import Record

_NBSP = " "
_CHAT = 9_999
_USER = 4_242  # deliberately different from the chat id
_WB_LINK = "https://www.wildberries.ru/catalog/12345678/detail.aspx"
_NAME = "Кроссовки <b>x*"
_CARD_ID = 5  # message_id of the card in ``_CONFIRMING_DATA``
_PRICE = 199_000  # 1 990 rubles


class _TrackingStub:
    """Records ``TrackingService`` calls; raises or returns what is set."""

    def __init__(self) -> None:
        self.init_kwargs: list[dict[str, Any]] = []
        self.preview_calls: list[tuple[int, Marketplace, int]] = []
        self.add_calls: list[tuple[Any, ...]] = []
        self.preview_result: Any = MarketplaceProductData(name=_NAME, price=_PRICE)
        self.preview_error: Exception | None = None
        self.add_error: Exception | None = None
        self.add_direction = AlertDirection.below

    async def preview(
        self, tg_user_id: int, marketplace: Marketplace, article: int
    ) -> Any:
        self.preview_calls.append((tg_user_id, marketplace, article))
        if self.preview_error is not None:
            raise self.preview_error
        return self.preview_result

    async def add_tracking(self, *args: Any) -> Alert:
        self.add_calls.append(args)
        if self.add_error is not None:
            raise self.add_error
        return Alert(
            id=1,
            user_id=1,
            product_id=1,
            target_price=args[5],
            direction=self.add_direction,
        )


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _TrackingStub:
    tracking = _TrackingStub()

    def build(**kwargs: Any) -> _TrackingStub:
        tracking.init_kwargs.append(kwargs)
        return tracking

    monkeypatch.setattr(add_product_module, "TrackingService", build)
    return tracking


class _Dialog:
    """Drives one user's dialog and exposes what the bot sent."""

    def __init__(self, dispatcher: Dispatcher, bot: Bot) -> None:
        self.dispatcher = dispatcher
        self.bot = bot
        self._update_id = 0

    def _next(self) -> int:
        self._update_id += 1
        return self._update_id

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def command(self, name: str) -> None:
        await self.feed(
            make_command_update(
                name, update_id=self._next(), chat_id=_CHAT, user_id=_USER
            )
        )

    async def say(self, text: str) -> None:
        await self.feed(
            make_message_update(
                text=text, update_id=self._next(), chat_id=_CHAT, user_id=_USER
            )
        )

    async def press(
        self,
        data: str,
        *,
        accessible: bool = True,
        message_id: int = _CARD_ID,
        with_message: bool = True,
    ) -> None:
        await self.feed(
            make_callback_update(
                data,
                update_id=self._next(),
                chat_id=_CHAT,
                user_id=_USER,
                accessible=accessible,
                message_id=message_id,
                with_message=with_message,
            )
        )

    @property
    def requests(self) -> list[Any]:
        return cast(Any, self.bot.session).requests  # type: ignore[no-any-return]

    @property
    def sent(self) -> list[SendMessage]:
        return [r for r in self.requests if isinstance(r, SendMessage)]

    @property
    def callback_answers(self) -> list[AnswerCallbackQuery]:
        return [r for r in self.requests if isinstance(r, AnswerCallbackQuery)]

    @property
    def last_text(self) -> str:
        return self.sent[-1].text

    @property
    def key(self) -> StorageKey:
        return StorageKey(bot_id=self.bot.id, chat_id=_CHAT, user_id=_USER)

    async def state(self) -> str | None:
        return await self.dispatcher.storage.get_state(self.key)

    async def data(self) -> dict[str, Any]:
        return await self.dispatcher.storage.get_data(self.key)

    async def set_state(
        self, state: str | None, data: dict[str, Any] | None = None
    ) -> None:
        await self.dispatcher.storage.set_state(self.key, state)
        await self.dispatcher.storage.set_data(self.key, data or {})


@pytest.fixture
def dialog(real_dispatcher: Dispatcher) -> _Dialog:
    bot = build_fake_bot(responder=send_message_responder())
    return _Dialog(real_dispatcher, bot)


_CONFIRMING_DATA: dict[str, Any] = {
    "marketplace": "wb",
    "article": 12345678,
    "product_name": _NAME,
    "current_price": _PRICE,
    "target_price": 150_000,
    "confirm_message_id": _CARD_ID,
}


async def _at_confirming(dialog: _Dialog) -> None:
    await dialog.set_state(AddProduct.confirming.state, dict(_CONFIRMING_DATA))


def _keyboard_callbacks(request: SendMessage) -> list[str | None]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


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
async def test_ozon_link_is_reported_unsupported_not_not_found(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_error = MarketplaceNotSupportedError("ozon")
    await dialog.command("add")

    await dialog.say("https://www.ozon.ru/product/slug-1234567/")

    assert stub.preview_calls == [(_USER, Marketplace.ozon, 1234567)]
    assert dialog.last_text == texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT
    assert "не поддерживается" in dialog.last_text
    assert (
        dialog.last_text != texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.not_found]
    )
    assert await dialog.state() == AddProduct.waiting_link.state


@pytest.mark.asyncio
async def test_preview_limit_shows_the_limit_and_clears_the_state(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    stub.preview_error = ProductLimitExceededError(37)
    await dialog.command("add")

    await dialog.say(_WB_LINK)

    assert "37" in dialog.last_text
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

    assert keyword in dialog.last_text
    assert "SECRET-DETAIL-MARKER" not in dialog.last_text
    assert await dialog.state() == AddProduct.waiting_link.state
    assert _keyboard_callbacks(dialog.sent[-1]) == [CB_ADD_CANCEL]


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

    await dialog.say(_WB_LINK)

    card = dialog.sent[-1]
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
        (DuplicateAlertError(), texts.ADD_DUPLICATE_TEXT),
        (ProductLimitExceededError(37), texts.limit_exceeded_text(37)),
        (
            MarketplaceNotSupportedError("ozon"),
            texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT,
        ),
    ],
    ids=["duplicate", "limit", "unsupported"],
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
    assert texts.limit_exceeded_text(37) != texts.ADD_DUPLICATE_TEXT
    assert texts.ADD_DUPLICATE_TEXT != texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT


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


@pytest.mark.asyncio
async def test_cancel_button_on_an_inaccessible_message_still_answers_the_callback(
    dialog: _Dialog, stub: _TrackingStub
) -> None:
    await dialog.set_state(AddProduct.waiting_link.state)

    await dialog.press(CB_ADD_CANCEL, accessible=False)

    assert len(dialog.callback_answers) == 1
    assert dialog.sent == []
    assert await dialog.state() is None


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
    )
    dialog = _Dialog(dispatcher, build_fake_bot(responder=send_message_responder()))
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
    dispatcher = create_dispatcher(
        MemoryStorage(), async_sessionmaker(class_=AsyncSession), http_client, 17
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
        assert kwargs["client_factory"] is get_client
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
