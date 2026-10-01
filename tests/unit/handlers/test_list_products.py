"""Tests for the /list handlers, app.handlers.list_products (PAB-070).

Everything goes through ``Dispatcher.feed_update`` on the real
``create_dispatcher()`` output with a fake ``Bot`` session, so the router
order (Р6), the middleware wiring and the aiogram ``CallbackData`` filters are
exercised too. The replaced boundary is ``TrackingService``: a
``MagicMock(spec=TrackingService)`` returns what a scenario programs and
records every call. Business rules are covered by the service tests.
"""

from __future__ import annotations

from typing import Any
from typing import cast
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Update

import app.handlers.add_product as add_product_module
from app.bot import texts
from app.bot.states import AddProduct
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.services.tracking import AlertRemoval
from app.services.tracking import ProductCard
from app.services.tracking import TrackingService
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import make_callback_update
from tests.unit.conftest import make_command_update
from tests.unit.conftest import send_message_responder

_CHAT = 9_999
_USER = 4_242  # deliberately different from the chat id
_MAX_ID = 2_147_483_647
_NAME = "Кроссовки <b>x*"
_NB = "\u00a0"


def _product(product_id: int, name: str = _NAME, price: int = 199_000) -> Product:
    return Product(
        id=product_id,
        user_id=1,
        marketplace=Marketplace.wb,
        article=12345678,
        product_name=name,
        current_price=price,
    )


def _alert(alert_id: int, direction: AlertDirection, target: int) -> Alert:
    return Alert(
        id=alert_id, user_id=1, product_id=5, target_price=target, direction=direction
    )


class _Bot:
    """Feeds updates of one user and exposes what the bot sent."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        self.bot: Bot = build_fake_bot(responder=send_message_responder())
        self._update_id = 0

    async def feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def command(self, name: str) -> None:
        self._update_id += 1
        await self.feed(
            make_command_update(
                name, update_id=self._update_id, chat_id=_CHAT, user_id=_USER
            )
        )

    async def press(
        self, data: str, *, accessible: bool = True, with_message: bool = True
    ) -> None:
        self._update_id += 1
        await self.feed(
            make_callback_update(
                data,
                update_id=self._update_id,
                chat_id=_CHAT,
                user_id=_USER,
                accessible=accessible,
                with_message=with_message,
            )
        )

    @property
    def sent(self) -> list[SendMessage]:
        requests = cast(Any, self.bot.session).requests
        return [r for r in requests if isinstance(r, SendMessage)]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        requests = cast(Any, self.bot.session).requests
        return [r for r in requests if isinstance(r, AnswerCallbackQuery)]

    @property
    def key(self) -> StorageKey:
        return StorageKey(bot_id=self.bot.id, chat_id=_CHAT, user_id=_USER)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(spec=TrackingService)
    mock.list_products.return_value = []
    mock.get_product_card.return_value = None
    mock.remove_product.return_value = False
    mock.remove_alert.return_value = AlertRemoval.not_found
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: mock)
    return mock


@pytest.fixture
def bot(real_dispatcher: Dispatcher, service: MagicMock) -> _Bot:
    return _Bot(real_dispatcher)


_PRESS: dict[str, dict[str, bool]] = {
    "message": {},
    "inaccessible": {"accessible": False},
    "none": {"with_message": False},
}
_KINDS_WITH_CHAT = ["message", "inaccessible"]


def _no_service_call(service: MagicMock) -> None:
    service.list_products.assert_not_called()
    service.get_product_card.assert_not_called()
    service.remove_product.assert_not_called()
    service.remove_alert.assert_not_called()


def _keyboard(request: SendMessage) -> InlineKeyboardMarkup:
    assert isinstance(request.reply_markup, InlineKeyboardMarkup)
    return request.reply_markup


# --- /list ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_with_no_products_hints_at_add_without_keyboard(
    bot: _Bot, service: MagicMock
) -> None:
    await bot.command("list")

    assert [r.text for r in bot.sent] == [texts.LIST_EMPTY_TEXT]
    assert bot.sent[0].reply_markup is None
    service.list_products.assert_awaited_once_with(_USER)


@pytest.mark.asyncio
async def test_list_shows_one_button_per_product_and_one_disclaimer(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(5), _product(9, "Шапка", 50)]

    await bot.command("list")

    request = bot.sent[0]
    assert request.text.count(PRICE_DISCLAIMER_SHORT) == 1
    assert request.text.startswith("Вы следите за товарами: 2.")
    assert "Показаны первые" not in request.text
    assert request.parse_mode is None
    rows = _keyboard(request).inline_keyboard
    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(f"{_NAME} — 1{_NB}990{_NB}₽", "pcard:5")],
        [(f"Шапка — 0,50{_NB}₽", "pcard:9")],
    ]


@pytest.mark.asyncio
async def test_list_with_exactly_fifty_products_shows_fifty_and_no_cut_note(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(i) for i in range(1, 51)]

    await bot.command("list")

    assert len(_keyboard(bot.sent[0]).inline_keyboard) == 50
    assert "Показаны первые" not in bot.sent[0].text
    assert "товарами: 50." in bot.sent[0].text


@pytest.mark.asyncio
async def test_list_with_fifty_one_products_shows_fifty_and_names_the_cut(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(i) for i in range(1, 52)]

    await bot.command("list")

    assert len(_keyboard(bot.sent[0]).inline_keyboard) == 50
    assert "Показаны первые 50 из 51" in bot.sent[0].text


@pytest.mark.asyncio
async def test_list_from_a_message_without_sender_is_ignored(
    bot: _Bot, service: MagicMock
) -> None:
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": _CHAT, "type": "private"},
                "text": "/list",
            },
        }
    )

    await bot.feed(update)

    assert bot.sent == []
    service.list_products.assert_not_called()


_DIALOG_STATES = [
    (
        AddProduct.waiting_link.state,
        {},
    ),
    (
        AddProduct.waiting_target_price.state,
        {"marketplace": "wb", "article": 1, "product_name": "x", "current_price": 5},
    ),
    (
        AddProduct.confirming.state,
        {
            "marketplace": "wb",
            "article": 1,
            "product_name": "x",
            "current_price": 5,
            "target_price": 3,
            "confirm_message_id": 5,
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "data"), _DIALOG_STATES)
async def test_list_inside_an_add_dialog_state_answers_and_keeps_state_and_draft(
    bot: _Bot, service: MagicMock, state: str, data: dict[str, Any]
) -> None:
    service.list_products.return_value = [_product(5)]
    storage = bot.dispatcher.storage
    await storage.set_state(bot.key, state)
    await storage.set_data(bot.key, dict(data))

    await bot.command("list")

    assert [_keyboard(r).inline_keyboard[0][0].callback_data for r in bot.sent] == [
        "pcard:5"
    ]
    assert await storage.get_state(bot.key) == state
    assert await storage.get_data(bot.key) == data
    # No /add handler ran: they would call preview/add_tracking or reply.
    service.preview.assert_not_called()
    service.add_tracking.assert_not_called()


# --- card ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_card_sends_a_new_plain_text_message_with_all_card_parts(
    bot: _Bot, service: MagicMock
) -> None:
    service.get_product_card.return_value = ProductCard(
        product=_product(5),
        alerts=(
            _alert(11, AlertDirection.below, 150_000),
            _alert(12, AlertDirection.above, 250_000),
        ),
    )

    await bot.press("pcard:5")

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert len(bot.answers) == 1
    assert bot.answers[0].show_alert is None
    assert bot.answers[0].text is None
    request = bot.sent[0]
    assert request.parse_mode is None
    assert request.text == (
        f"{_NAME}\n"
        "Маркетплейс: Wildberries\n"
        "https://www.wildberries.ru/catalog/12345678/detail.aspx\n"
        f"Текущая цена: 1{_NB}990{_NB}₽\n"
        "Цена без учёта персональных скидок.\n"
        "\n"
        "Пороги:\n"
        f"• Сообщу, когда цена станет ниже 1{_NB}500{_NB}₽\n"
        f"• Сообщу, когда цена станет выше 2{_NB}500{_NB}₽"
    )
    assert request.text.count(PRICE_DISCLAIMER_SHORT) == 1
    rows = _keyboard(request).inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [
        ["adel:11"],
        ["adel:12"],
        ["pdel:5"],
    ]


@pytest.mark.asyncio
async def test_open_card_of_a_missing_product_alerts_the_gone_text_only(
    bot: _Bot, service: MagicMock
) -> None:
    await bot.press("pcard:5")

    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_GONE_ALERT_TEXT, True)
    ]
    assert bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS_WITH_CHAT)
async def test_open_card_on_a_message_with_a_chat_sends_the_card_there_and_answers_once(
    bot: _Bot, service: MagicMock, kind: str
) -> None:
    """``message`` is the positive control; ``inaccessible`` is a stale button."""
    service.get_product_card.return_value = ProductCard(
        product=_product(5), alerts=(_alert(11, AlertDirection.below, 150_000),)
    )

    await bot.press("pcard:5", **_PRESS[kind])

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert [(a.text, a.show_alert) for a in bot.answers] == [(None, None)]
    assert len(bot.sent) == 1
    request = bot.sent[0]
    assert request.chat_id == _CHAT
    assert request.parse_mode is None
    assert request.text.startswith(f"{_NAME}\nМаркетплейс: Wildberries\n")
    assert f"• Сообщу, когда цена станет ниже 1{_NB}500{_NB}₽" in request.text
    rows = _keyboard(request).inline_keyboard
    assert [[b.callback_data for b in row] for row in rows] == [["adel:11"], ["pdel:5"]]


@pytest.mark.asyncio
async def test_open_card_without_any_message_pops_up_the_stale_alert_and_sends_nothing(
    bot: _Bot, service: MagicMock
) -> None:
    service.get_product_card.return_value = ProductCard(product=_product(5), alerts=())

    await bot.press("pcard:5", **_PRESS["none"])

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert bot.sent == []


# --- removal ---------------------------------------------------------------


_REMOVE_ALERT_OUTCOMES = [
    (AlertRemoval.removed, texts.LIST_ALERT_REMOVED_TEXT),
    (AlertRemoval.removed_with_product, texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "text"), _REMOVE_ALERT_OUTCOMES)
async def test_remove_alert_success_answers_once_and_sends_its_own_plain_text(
    bot: _Bot, service: MagicMock, outcome: AlertRemoval, text: str
) -> None:
    service.remove_alert.return_value = outcome

    await bot.press("adel:11")

    service.remove_alert.assert_awaited_once_with(_USER, 11)
    assert len(bot.answers) == 1
    assert [(r.text, r.parse_mode) for r in bot.sent] == [(text, None)]


@pytest.mark.asyncio
async def test_remove_alert_not_found_alerts_the_gone_text_once_and_sends_nothing(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_alert.return_value = AlertRemoval.not_found

    await bot.press("adel:11")

    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_GONE_ALERT_TEXT, True)
    ]
    assert bot.sent == []


_REMOVE_CASES = [
    ("adel:11", "remove_alert", AlertRemoval.removed, texts.LIST_ALERT_REMOVED_TEXT),
    (
        "adel:11",
        "remove_alert",
        AlertRemoval.removed_with_product,
        texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
    ),
    ("pdel:5", "remove_product", True, texts.LIST_PRODUCT_REMOVED_TEXT),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _KINDS_WITH_CHAT)
@pytest.mark.parametrize(("data", "method", "result", "text"), _REMOVE_CASES)
async def test_removal_outcome_on_a_message_with_a_chat_is_sent_there_and_answered_once(
    bot: _Bot,
    service: MagicMock,
    kind: str,
    data: str,
    method: str,
    result: object,
    text: str,
) -> None:
    """``message`` is the positive control; ``inaccessible`` is a stale button."""
    getattr(service, method).return_value = result

    await bot.press(data, **_PRESS[kind])

    getattr(service, method).assert_awaited_once()
    assert [(a.text, a.show_alert) for a in bot.answers] == [(None, None)]
    assert [(r.chat_id, r.text, r.parse_mode, r.reply_markup) for r in bot.sent] == [
        (_CHAT, text, None, None)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("data", "method", "result", "text"), _REMOVE_CASES)
async def test_removal_outcome_without_any_message_pops_up_the_outcome_text(
    bot: _Bot, service: MagicMock, data: str, method: str, result: object, text: str
) -> None:
    getattr(service, method).return_value = result

    await bot.press(data, **_PRESS["none"])

    getattr(service, method).assert_awaited_once()
    assert [(a.text, a.show_alert) for a in bot.answers] == [(text, True)]
    assert bot.sent == []


@pytest.mark.asyncio
async def test_remove_product_success_answers_once_and_names_how_to_return(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_product.return_value = True

    await bot.press("pdel:5")

    service.remove_product.assert_awaited_once_with(_USER, 5)
    assert len(bot.answers) == 1
    assert [(r.text, r.parse_mode) for r in bot.sent] == [
        (texts.LIST_PRODUCT_REMOVED_TEXT, None)
    ]
    assert "/add" in bot.sent[0].text


@pytest.mark.asyncio
async def test_remove_product_failure_alerts_the_gone_text_once_and_sends_nothing(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_product.return_value = False

    await bot.press("pdel:5")

    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_GONE_ALERT_TEXT, True)
    ]
    assert bot.sent == []


@pytest.mark.asyncio
async def test_foreign_and_missing_rows_get_the_same_text_on_every_path(
    real_dispatcher: Dispatcher, service: MagicMock
) -> None:
    """Both look identical to the handler (``None``/``False``/``not_found``);
    the same alert text must reach the user for card, alert and product."""
    texts_seen: list[str | None] = []
    for data in ("pcard:5", "adel:5", "pdel:5"):
        one = _Bot(real_dispatcher)
        await one.press(data)
        texts_seen.extend(a.text for a in one.answers)

    assert texts_seen == [texts.LIST_GONE_ALERT_TEXT] * 3


@pytest.mark.asyncio
async def test_service_calls_are_scoped_to_the_pressing_user_not_the_chat(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_product.return_value = True
    service.remove_alert.return_value = AlertRemoval.removed

    await bot.press("pcard:1")
    await bot.press("adel:2")
    await bot.press("pdel:3")

    service.get_product_card.assert_awaited_once_with(_USER, 1)
    service.remove_alert.assert_awaited_once_with(_USER, 2)
    service.remove_product.assert_awaited_once_with(_USER, 3)


# --- forged callbacks (Р5) ---------------------------------------------------

_FORGED_SUFFIXES = [
    "0",
    "-1",
    "2147483648",
    "abc",
    "1:2",  # extra field
    "",  # missing field (``pcard:``)
    "1.5",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["pcard", "adel", "pdel"])
@pytest.mark.parametrize("suffix", _FORGED_SUFFIXES)
async def test_forged_callback_never_reaches_the_service_and_gets_one_stale_alert(
    bot: _Bot, service: MagicMock, prefix: str, suffix: str
) -> None:
    await bot.press(f"{prefix}:{suffix}")

    _no_service_call(service)
    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["pcard", "adel", "pdel"])
async def test_callback_without_any_id_part_is_stale_and_never_reaches_the_service(
    bot: _Bot, service: MagicMock, data: str
) -> None:
    await bot.press(data)

    _no_service_call(service)
    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]


@pytest.mark.asyncio
async def test_largest_valid_id_passes_through_on_all_three_prefixes(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_product.return_value = True
    service.remove_alert.return_value = AlertRemoval.removed

    await bot.press(f"pcard:{_MAX_ID}")
    await bot.press(f"adel:{_MAX_ID}")
    await bot.press(f"pdel:{_MAX_ID}")

    service.get_product_card.assert_awaited_once_with(_USER, _MAX_ID)
    service.remove_alert.assert_awaited_once_with(_USER, _MAX_ID)
    service.remove_product.assert_awaited_once_with(_USER, _MAX_ID)


@pytest.mark.asyncio
async def test_unrelated_callback_data_is_not_swallowed_by_the_stale_trap(
    bot: _Bot, service: MagicMock
) -> None:
    """``add:confirm`` outside a dialog is the /add handlers' business."""
    await bot.press("add:confirm")

    assert [a.text for a in bot.answers] == [texts.ADD_STALE_ALERT_TEXT]
    _no_service_call(service)


# --- exceptions are not swallowed ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "method"),
    [
        ("pcard:5", "get_product_card"),
        ("adel:5", "remove_alert"),
        ("pdel:5", "remove_product"),
    ],
)
async def test_unexpected_service_failure_reaches_the_errors_handler(
    bot: _Bot, service: MagicMock, data: str, method: str
) -> None:
    getattr(service, method).side_effect = RuntimeError("boom")

    await bot.press(data)

    assert [a.text for a in bot.answers] == [None]  # errors handler answers once
    assert [r.text for r in bot.sent] == [texts.UNEXPECTED_ERROR_TEXT]


@pytest.mark.asyncio
async def test_unknown_removal_outcome_is_an_error_not_a_silent_success(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_alert.return_value = "vanished"

    await bot.press("adel:5")

    assert [r.text for r in bot.sent] == [texts.UNEXPECTED_ERROR_TEXT]
