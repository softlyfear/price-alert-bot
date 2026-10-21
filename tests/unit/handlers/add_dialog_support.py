"""Shared doubles and constants of the /add dialog tests (PAB-095, tail (b) of PAB-094).

Moved verbatim from ``test_add_product.py`` so that other handler tests import
them from here instead of from another test module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.client.default import Default
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message
from aiogram.types import Update

from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.states import AddProduct
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from tests.unit.conftest import FakeTelegramSession
from tests.unit.conftest import make_callback_update
from tests.unit.conftest import make_command_update
from tests.unit.conftest import make_message_update
from tests.unit.conftest import send_message_responder

if TYPE_CHECKING:
    pass

_CHAT = 9_999
_USER = 4_242  # deliberately different from the chat id

_NAME = "Кроссовки <b>x*"
_CARD_ID = 5  # message_id of the card in ``_CONFIRMING_DATA``
_PRICE = 199_000  # 1 990 rubles
_MAIN_MENU: list[str | None] = [
    "menu:add",
    "menu:list",
    "menu:help",
]  # callback_data of the main menu


class _TrackingStub:
    """Records ``TrackingService`` calls; raises or returns what is set."""

    def __init__(self) -> None:
        self.init_kwargs: list[dict[str, Any]] = []
        self.preview_calls: list[tuple[int, Marketplace, int]] = []
        self.add_calls: list[tuple[Any, ...]] = []
        self.add_fetched_at: list[datetime | None] = []
        self.preview_result: Any = MarketplaceProductData(name=_NAME, price=_PRICE)
        self.preview_error: Exception | None = None
        self.add_error: Exception | None = None
        self.add_direction = AlertDirection.below
        self.on_preview: Callable[[], None] | None = None

    async def find_tracked_product(
        self, tg_user_id: int, marketplace: Marketplace, article: int
    ) -> None:
        """The user tracks nothing: the link step goes on to ``preview``."""
        return None

    async def preview(
        self, tg_user_id: int, marketplace: Marketplace, article: int
    ) -> Any:
        self.preview_calls.append((tg_user_id, marketplace, article))
        if self.on_preview is not None:
            self.on_preview()
        if self.preview_error is not None:
            raise self.preview_error
        return self.preview_result

    async def add_tracking(
        self, *args: Any, price_fetched_at: datetime | None
    ) -> Alert:
        self.add_calls.append(args)
        self.add_fetched_at.append(price_fetched_at)
        if self.add_error is not None:
            raise self.add_error
        return Alert(
            id=1,
            user_id=1,
            product_id=1,
            target_price=args[5],
            direction=self.add_direction,
        )


class _Dialog:
    """Drives one user's dialog and exposes what the bot sent."""

    def __init__(self, dispatcher: Dispatcher, bot: Bot, faults: _Faults) -> None:
        self.dispatcher = dispatcher
        self.bot = bot
        self.faults = faults
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

    async def say(self, text: str, *, message_id: int = 1) -> None:
        await self.feed(
            make_message_update(
                text=text,
                update_id=self._next(),
                chat_id=_CHAT,
                user_id=_USER,
                message_id=message_id,
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
    def edits(self) -> list[EditMessageText]:
        return [r for r in self.requests if isinstance(r, EditMessageText)]

    @property
    def outcome(self) -> SendMessage | EditMessageText:
        """The last message-text request: how the step's outcome was shown."""
        shown = [
            r for r in self.requests if isinstance(r, (SendMessage, EditMessageText))
        ]
        return shown[-1]

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


class _Faults:
    """Bot API failures the fake session raises on demand."""

    def __init__(self) -> None:
        self.next_send_error: Exception | None = None  # one-shot
        self.edit_error: Exception | None = None


class _BoundSession(FakeTelegramSession):
    """Fake session that mounts returned messages on the bot, like the real one.

    ``Message.edit_text`` needs a bot-bound message; a raw model from the
    responder would fail with "not mounted to a bot instance".
    """

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 -- overrides BaseSession's signature
    ) -> Any:
        result: Any = await super().make_request(bot, method, timeout)
        if isinstance(result, Message):
            result.as_(bot)
        return result


def _responder(faults: _Faults) -> Callable[[TelegramMethod[Any]], Any]:
    sends = send_message_responder()

    async def respond(method: TelegramMethod[Any]) -> Any:
        if isinstance(method, SendMessage):
            if faults.next_send_error is not None:
                error, faults.next_send_error = faults.next_send_error, None
                raise error
            return await sends(method)
        if isinstance(method, EditMessageText):
            if faults.edit_error is not None:
                raise faults.edit_error
            return Message.model_validate(
                {
                    "message_id": method.message_id,
                    "date": 1,
                    "chat": {"id": method.chat_id, "type": "private"},
                }
            )
        return await sends(method)

    return respond


_CONFIRMING_DATA: dict[str, Any] = {
    "marketplace": "wb",
    "article": 12345678,
    "product_name": _NAME,
    "current_price": _PRICE,
    "target_price": 150_000,
    "confirm_message_id": _CARD_ID,
}


def _keyboard_callbacks(request: SendMessage | EditMessageText) -> list[str | None]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class _Outcome:
    """One of the four ways the link step ends, with what the user must see."""

    def __init__(
        self,
        configure: Callable[[_TrackingStub], None],
        text: str,
        *,
        keyboard: list[str | None] | None,
        plain: bool,
        state: str | None,
    ) -> None:
        self.configure = configure
        self.text = text
        self.keyboard = keyboard
        self.plain = plain  # card: parse_mode=None; others: the bot default
        self.state = state


def _limit(stub: _TrackingStub) -> None:
    stub.preview_error = ProductLimitExceededError(37)


def _unsupported(stub: _TrackingStub) -> None:
    stub.preview_error = MarketplaceNotSupportedError("ozon")


def _blocked(stub: _TrackingStub) -> None:
    stub.preview_result = MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="d"
    )


def _card(stub: _TrackingStub) -> None:
    """Default stub result: a found product."""


_OUTCOMES = [
    pytest.param(
        _Outcome(
            _limit,
            texts.limit_exceeded_text(37),
            keyboard=_MAIN_MENU,
            plain=False,
            state=None,
        ),
        id="limit",
    ),
    pytest.param(
        _Outcome(
            _unsupported,
            texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT,
            keyboard=[CB_ADD_CANCEL],
            plain=False,
            state=AddProduct.waiting_link.state,
        ),
        id="unsupported",
    ),
    pytest.param(
        _Outcome(
            _blocked,
            texts.ADD_FETCH_FAILURE_TEXTS[FetchFailureReason.blocked],
            keyboard=[CB_ADD_CANCEL],
            plain=False,
            state=AddProduct.waiting_link.state,
        ),
        id="fetch-failure",
    ),
    pytest.param(
        _Outcome(
            _card,
            texts.product_found_text(_NAME, _PRICE),
            keyboard=[CB_ADD_CANCEL],
            plain=True,
            state=AddProduct.waiting_target_price.state,
        ),
        id="card",
    ),
]


def _assert_shown_as(request: SendMessage | EditMessageText, outcome: _Outcome) -> None:
    assert request.text == outcome.text
    if outcome.keyboard is None:
        assert request.reply_markup is None
    else:
        assert _keyboard_callbacks(request) == outcome.keyboard
    if outcome.plain:
        assert request.parse_mode is None
    else:
        assert isinstance(request.parse_mode, Default)


_LINK_A = "https://www.wildberries.ru/catalog/11111111/detail.aspx"

_NAME_A = "Товар-A"
_NAME_B = "Товар-B"
_MSG_A = 101  # message_id of the first link message

_WAIT = 5.0  # failsafe against a hung test, never used to synchronise


class _Call:
    """One scripted ``preview`` call; ``release`` (if set) holds it open."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.result: Any = None
        self.error: Exception | None = None


class _GatedTracking(_TrackingStub):
    """``preview`` double whose n-th call can wait for an explicit ``Event``."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: dict[int, _Call] = {}

    def script(
        self,
        index: int,
        *,
        gated: bool = True,
        result: Any = None,
        error: Exception | None = None,
    ) -> _Call:
        call = _Call()
        call.release = asyncio.Event() if gated else None
        call.result = result
        call.error = error
        self.calls[index] = call
        return call

    async def preview(
        self, tg_user_id: int, marketplace: Marketplace, article: int
    ) -> Any:
        call = self.calls.setdefault(len(self.preview_calls), _Call())
        self.preview_calls.append((tg_user_id, marketplace, article))
        call.entered.set()
        if call.release is not None:
            await call.release.wait()
        if call.error is not None:
            raise call.error
        if call.result is not None:
            return call.result
        return MarketplaceProductData(name=_NAME, price=_PRICE)


def _assert_only_dropped_edit(dialog: _Dialog, base: int, notice_id: int) -> None:
    step = dialog.requests[base:]
    assert [type(r) for r in step] == [EditMessageText]
    edit = step[0]
    assert isinstance(edit, EditMessageText)
    assert edit.text == texts.ADD_CHECK_DROPPED_TEXT
    assert (edit.chat_id, edit.message_id) == (_CHAT, notice_id)
    assert edit.reply_markup is None
    assert edit.parse_mode is None


def _shown_texts(dialog: _Dialog) -> str:
    return "\n".join(
        r.text for r in dialog.requests if isinstance(r, (SendMessage, EditMessageText))
    )


def _without_stamp(
    data: dict[str, Any], before: datetime, after: datetime
) -> dict[str, Any]:
    """Draft minus ``price_fetched_at``; stamp must be aware ISO in window."""
    raw = data["price_fetched_at"]
    assert isinstance(raw, str)
    stamp = datetime.fromisoformat(raw)
    assert stamp.utcoffset() is not None
    assert stamp.astimezone(UTC) == stamp
    assert before <= stamp <= after
    return {k: v for k, v in data.items() if k != "price_fetched_at"}
