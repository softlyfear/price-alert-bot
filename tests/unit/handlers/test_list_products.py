"""Tests for the /list handlers, app.handlers.list_products (PAB-070).

Everything goes through ``Dispatcher.feed_update`` on the real
``create_dispatcher()`` output with a fake ``Bot`` session, so the router
order (Р6), the middleware wiring and the aiogram ``CallbackData`` filters are
exercised too. The replaced boundary is ``TrackingService``: a
``MagicMock(spec=TrackingService)`` returns what a scenario programs and
records every call. Business rules are covered by the service tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import call

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.methods import TelegramMethod
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

if TYPE_CHECKING:
    from loguru import Record

_CHAT = 9_999
_USER = 4_242  # deliberately different from the chat id
_MAX_ID = 2_147_483_647
_NAME = "Кроссовки <b>x*"
_NB = "\u00a0"
_NAV_ROW = [("➕ Добавить товар", "menu:add"), ("Главное меню", "menu:start")]
_CARD_NAV = [("← К списку", "menu:list"), ("Главное меню", "menu:start")]
_LEGEND = "↓ — сообщу, когда цена снизится до порога, ↑ — когда вырастет до него."
_CARD_HEAD = (
    f"{_NAME}\n"
    "Маркетплейс: Wildberries\n"
    "https://www.wildberries.ru/catalog/12345678/detail.aspx\n"
    f"Текущая цена: 1{_NB}990{_NB}₽\n"
    "Цена без учёта персональных скидок.\n"
    "\n"
    "Пороги:\n"
)
_CARD_ONE_ALERT = f"{_CARD_HEAD}• ↓ 1{_NB}500{_NB}₽\n{_LEGEND}"


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
    def edits(self) -> list[EditMessageText]:
        requests = cast(Any, self.bot.session).requests
        return [r for r in requests if isinstance(r, EditMessageText)]

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


def _keyboard(request: SendMessage | EditMessageText) -> InlineKeyboardMarkup:
    assert isinstance(request.reply_markup, InlineKeyboardMarkup)
    return request.reply_markup


# --- /list ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_with_no_products_shows_only_the_navigation_row(
    bot: _Bot, service: MagicMock
) -> None:
    await bot.command("list")

    assert [r.text for r in bot.sent] == [texts.LIST_EMPTY_TEXT]
    rows = _keyboard(bot.sent[0]).inline_keyboard
    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [_NAV_ROW]
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
        _NAV_ROW,
    ]


@pytest.mark.asyncio
async def test_list_with_exactly_fifty_products_shows_fifty_and_no_cut_note(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(i) for i in range(1, 51)]

    await bot.command("list")

    rows = _keyboard(bot.sent[0]).inline_keyboard
    assert len(rows) == 50 + 1
    assert rows[49][0].callback_data == "pcard:50"
    assert [(b.text, b.callback_data) for b in rows[-1]] == _NAV_ROW
    assert "Показаны первые" not in bot.sent[0].text
    assert "товарами: 50." in bot.sent[0].text


@pytest.mark.asyncio
async def test_list_with_fifty_one_products_shows_fifty_and_names_the_cut(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(i) for i in range(1, 52)]

    await bot.command("list")

    rows = _keyboard(bot.sent[0]).inline_keyboard
    assert len(rows) == 50 + 1
    assert rows[49][0].callback_data == "pcard:50"
    assert [(b.text, b.callback_data) for b in rows[-1]] == _NAV_ROW
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


# --- "My products" menu button (PAB-092) -------------------------------------


@pytest.mark.asyncio
async def test_menu_list_button_asks_the_service_for_the_presser_not_the_chat(
    bot: _Bot, service: MagicMock
) -> None:
    await bot.press("menu:list")

    service.list_products.assert_awaited_once_with(_USER)


@pytest.mark.asyncio
async def test_menu_list_button_edits_in_place_with_the_same_screen_as_the_command(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(5), _product(9, "Шапка", 50)]
    await bot.command("list")

    await bot.press("menu:list")

    assert bot.edits[0].parse_mode is None
    assert len(bot.sent) == 1  # only the command's message; the button sent none
    assert bot.edits[0].text == bot.sent[0].text
    assert bot.edits[0].reply_markup == bot.sent[0].reply_markup
    rows = _keyboard(bot.edits[0]).inline_keyboard
    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [
        [(f"{_NAME} — 1{_NB}990{_NB}₽", "pcard:5")],
        [(f"Шапка — 0,50{_NB}₽", "pcard:9")],
        _NAV_ROW,
    ]
    assert len(bot.answers) == 1


@pytest.mark.asyncio
async def test_menu_list_button_with_no_products_shows_empty_text_and_nav_row(
    bot: _Bot, service: MagicMock
) -> None:
    await bot.press("menu:list")

    assert [e.text for e in bot.edits] == [texts.LIST_EMPTY_TEXT]
    rows = _keyboard(bot.edits[0]).inline_keyboard
    assert [[(b.text, b.callback_data) for b in row] for row in rows] == [_NAV_ROW]


@pytest.mark.asyncio
async def test_menu_list_button_still_cuts_the_list_at_fifty(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(i) for i in range(1, 52)]

    await bot.press("menu:list")

    rows = _keyboard(bot.edits[0]).inline_keyboard
    assert len(rows) == 50 + 1
    assert rows[49][0].callback_data == "pcard:50"
    assert "Показаны первые 50 из 51" in bot.edits[0].text


@pytest.mark.asyncio
async def test_menu_list_button_on_an_inaccessible_message_sends_the_list_as_new(
    bot: _Bot, service: MagicMock
) -> None:
    service.list_products.return_value = [_product(5)]

    await bot.press("menu:list", accessible=False)

    assert bot.edits == []
    assert [r.text.startswith("Вы следите за товарами: 1.") for r in bot.sent] == [True]
    assert len(bot.answers) == 1


@pytest.mark.asyncio
async def test_menu_list_button_without_a_message_gives_a_popup_and_no_message(
    bot: _Bot, service: MagicMock
) -> None:
    await bot.press("menu:list", with_message=False)

    assert bot.sent == []
    assert bot.edits == []
    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.MENU_STALE_ALERT_TEXT, True)
    ]


@pytest.mark.asyncio
async def test_menu_list_button_keeps_the_fsm_state_and_draft(
    bot: _Bot, service: MagicMock
) -> None:
    state, data = "some-state", {"k": 1}
    storage = bot.dispatcher.storage
    await storage.set_state(bot.key, state)
    await storage.set_data(bot.key, dict(data))

    await bot.press("menu:list")

    assert await storage.get_state(bot.key) == state
    assert await storage.get_data(bot.key) == data


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


def _rows(request: SendMessage | EditMessageText) -> list[list[tuple[str, str | None]]]:
    return [
        [(b.text, b.callback_data) for b in row]
        for row in _keyboard(request).inline_keyboard
    ]


def _one_alert_card() -> ProductCard:
    return ProductCard(
        product=_product(5), alerts=(_alert(11, AlertDirection.below, 150_000),)
    )


@pytest.mark.asyncio
async def test_open_card_edits_the_list_in_place_with_all_card_parts(
    bot: _Bot, service: MagicMock
) -> None:
    service.get_product_card.return_value = ProductCard(
        product=_product(5),
        alerts=(
            _alert(11, AlertDirection.below, 100_000),
            _alert(12, AlertDirection.above, 200_000),
        ),
    )

    await bot.press("pcard:5")

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert bot.sent == []
    assert [(a.text, a.show_alert) for a in bot.answers] == [(None, None)]
    request = bot.edits[0]
    assert request.parse_mode is None
    assert request.text == (
        f"{_CARD_HEAD}• ↓ 1{_NB}000{_NB}₽\n• ↑ 2{_NB}000{_NB}₽\n{_LEGEND}"
    )
    assert request.text.count(PRICE_DISCLAIMER_SHORT) == 1
    assert _rows(request) == [
        [(f"Изменить порог: 1{_NB}000{_NB}₽", "aedit:11:5")],
        [(f"Убрать порог: 1{_NB}000{_NB}₽", "adel:11:5")],
        [(f"Изменить порог: 2{_NB}000{_NB}₽", "aedit:12:5")],
        [(f"Убрать порог: 2{_NB}000{_NB}₽", "adel:12:5")],
        [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:5")],
        _CARD_NAV,
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
    assert bot.edits == []


@pytest.mark.asyncio
async def test_open_card_on_an_inaccessible_message_sends_the_card_as_new(
    bot: _Bot, service: MagicMock
) -> None:
    service.get_product_card.return_value = _one_alert_card()

    await bot.press("pcard:5", **_PRESS["inaccessible"])

    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert bot.edits == []
    assert [(a.text, a.show_alert) for a in bot.answers] == [(None, None)]
    assert [(r.chat_id, r.text, r.parse_mode) for r in bot.sent] == [
        (_CHAT, _CARD_ONE_ALERT, None)
    ]


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
    assert bot.edits == []


@pytest.mark.asyncio
async def test_back_to_list_button_of_the_card_edits_the_card_into_the_list(
    bot: _Bot, service: MagicMock
) -> None:
    """The button is taken from the real card keyboard, then pressed."""
    service.get_product_card.return_value = _one_alert_card()
    service.list_products.return_value = [_product(5)]
    await bot.press("pcard:5")
    back = _rows(bot.edits[0])[-1][0]
    assert back[0] == "← К списку"
    assert back[1] is not None

    await bot.press(back[1])

    service.list_products.assert_awaited_once_with(_USER)
    assert len(bot.edits) == 2
    assert bot.edits[1].text.startswith("Вы следите за товарами: 1.")
    assert bot.sent == []


# --- removal of a threshold --------------------------------------------------

_GONE = texts.PRODUCT_ALREADY_GONE_TEXT
_REMOVE_CASES = [
    pytest.param(
        AlertRemoval.removed,
        True,
        _CARD_ONE_ALERT,
        [
            [(f"Изменить порог: 1{_NB}500{_NB}₽", "aedit:11:5")],
            [(f"Убрать порог: 1{_NB}500{_NB}₽", "adel:11:5")],
            [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:5")],
            _CARD_NAV,
        ],
        texts.ALERT_REMOVED_TEXT,
        id="removed-card-redrawn",
    ),
    pytest.param(
        AlertRemoval.removed,
        False,
        "Порог удалён",
        [_CARD_NAV],
        None,
        id="removed-card-gone",
    ),
    pytest.param(
        AlertRemoval.not_found,
        True,
        _CARD_ONE_ALERT,
        [
            [(f"Изменить порог: 1{_NB}500{_NB}₽", "aedit:11:5")],
            [(f"Убрать порог: 1{_NB}500{_NB}₽", "adel:11:5")],
            [(texts.STOP_TRACKING_BUTTON_TEXT, "pdel:5")],
            _CARD_NAV,
        ],
        texts.ALERT_ALREADY_REMOVED_TEXT,
        id="not-found-card-redrawn",
    ),
    pytest.param(
        AlertRemoval.not_found,
        False,
        "Товар уже не отслеживается",
        [_CARD_NAV],
        None,
        id="not-found-card-gone",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "card_alive", "text", "rows", "toast"), _REMOVE_CASES
)
async def test_remove_alert_edits_the_card_in_place_and_answers_exactly_once(
    bot: _Bot,
    service: MagicMock,
    outcome: AlertRemoval,
    card_alive: bool,
    text: str,
    rows: list[list[tuple[str, str]]],
    toast: str | None,
) -> None:
    service.remove_alert.return_value = outcome
    service.get_product_card.return_value = _one_alert_card() if card_alive else None

    await bot.press("adel:11:5")

    service.remove_alert.assert_awaited_once_with(_USER, 11)
    service.get_product_card.assert_awaited_once_with(_USER, 5)
    assert bot.sent == []
    assert [(e.text, e.parse_mode) for e in bot.edits] == [(text, None)]
    assert _rows(bot.edits[0]) == rows
    assert [(a.text, a.show_alert) for a in bot.answers] == [(toast, None)]


@pytest.mark.asyncio
async def test_remove_alert_with_product_edits_to_the_summary_and_nav_row(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_alert.return_value = AlertRemoval.removed_with_product

    await bot.press("adel:11:5")

    service.remove_alert.assert_awaited_once_with(_USER, 11)
    service.get_product_card.assert_not_called()  # the product is gone with it
    assert bot.sent == []
    assert [(e.text, e.parse_mode) for e in bot.edits] == [
        (texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT, None)
    ]
    assert _rows(bot.edits[0]) == [_CARD_NAV]
    assert [(a.text, a.show_alert) for a in bot.answers] == [(None, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "card_alive", "popup"),
    [
        (AlertRemoval.removed, True, texts.ALERT_REMOVED_TEXT),
        (AlertRemoval.removed, False, texts.ALERT_REMOVED_TEXT),
        (
            AlertRemoval.removed_with_product,
            False,
            texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
        ),
        (AlertRemoval.not_found, True, texts.ALERT_ALREADY_REMOVED_TEXT),
        (AlertRemoval.not_found, False, texts.PRODUCT_ALREADY_GONE_TEXT),
    ],
)
async def test_remove_alert_without_any_message_pops_up_the_outcome_and_sends_nothing(
    bot: _Bot,
    service: MagicMock,
    outcome: AlertRemoval,
    card_alive: bool,
    popup: str,
) -> None:
    service.remove_alert.return_value = outcome
    service.get_product_card.return_value = _one_alert_card() if card_alive else None

    await bot.press("adel:11:5", **_PRESS["none"])

    assert [(a.text, a.show_alert) for a in bot.answers] == [(popup, True)]
    assert bot.sent == []
    assert bot.edits == []


@pytest.mark.asyncio
async def test_remove_alert_on_an_inaccessible_message_sends_the_card_as_new(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_alert.return_value = AlertRemoval.removed
    service.get_product_card.return_value = _one_alert_card()

    await bot.press("adel:11:5", **_PRESS["inaccessible"])

    assert bot.edits == []
    assert [(r.chat_id, r.text) for r in bot.sent] == [(_CHAT, _CARD_ONE_ALERT)]
    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.ALERT_REMOVED_TEXT, None)
    ]


@pytest.mark.asyncio
async def test_remove_alert_with_a_forged_product_id_leaks_no_card_data(
    bot: _Bot, service: MagicMock
) -> None:
    """A foreign or non-existent ``product_id`` looks like ``None`` to the
    handler: the card is read for the pressing user, so nothing is shown."""
    service.remove_alert.return_value = AlertRemoval.removed
    service.get_product_card.return_value = None

    await bot.press("adel:11:777")

    service.get_product_card.assert_awaited_once_with(_USER, 777)
    assert [e.text for e in bot.edits] == ["Порог удалён"]
    assert _rows(bot.edits[0]) == [_CARD_NAV]
    assert bot.sent == []
    assert not any(_NAME in (e.text or "") for e in bot.edits)


@pytest.mark.asyncio
async def test_remove_alert_swallows_a_failed_callback_answer_after_the_edit(
    real_dispatcher: Dispatcher, service: MagicMock, log_records: list[Record]
) -> None:
    """The DB middleware commits only if the handler returns normally."""
    inner = send_message_responder()

    async def responder(method: TelegramMethod[Any]) -> Any:
        if isinstance(method, AnswerCallbackQuery):
            raise TelegramBadRequest(method=method, message="Bad Request: too old")
        return await inner(method)

    one = _Bot(real_dispatcher)
    one.bot = build_fake_bot(responder=responder)
    service.remove_alert.return_value = AlertRemoval.removed
    service.get_product_card.return_value = _one_alert_card()

    await one.press("adel:11:5")  # must not raise

    assert [e.text for e in one.edits] == [_CARD_ONE_ALERT]
    assert one.sent == []  # no UNEXPECTED_ERROR_TEXT from the errors handler
    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0]["extra"]["error_type"] == "TelegramBadRequest"


@pytest.mark.asyncio
async def test_old_format_remove_button_is_stale_and_never_reaches_the_service(
    bot: _Bot, service: MagicMock
) -> None:
    await bot.press("adel:5")

    service.remove_alert.assert_not_called()
    service.get_product_card.assert_not_called()
    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert bot.sent == []
    assert bot.edits == []


# --- stop tracking -------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("removed", "text"),
    [
        (True, texts.LIST_PRODUCT_REMOVED_TEXT),
        (False, texts.PRODUCT_ALREADY_GONE_TEXT),
    ],
)
async def test_remove_product_edits_in_place_with_nav_row_and_no_popup(
    bot: _Bot, service: MagicMock, removed: bool, text: str
) -> None:
    service.remove_product.return_value = removed

    await bot.press("pdel:5")

    service.remove_product.assert_awaited_once_with(_USER, 5)
    assert [(e.text, e.parse_mode) for e in bot.edits] == [(text, None)]
    assert _rows(bot.edits[0]) == [_CARD_NAV]
    assert bot.sent == []
    assert [(a.text, a.show_alert) for a in bot.answers] == [(None, None)]


@pytest.mark.asyncio
async def test_remove_product_success_names_how_to_return(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_product.return_value = True

    await bot.press("pdel:5")

    assert "/add" in bot.edits[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("removed", [True, False])
async def test_remove_product_on_an_inaccessible_message_sends_the_outcome_as_new(
    bot: _Bot, service: MagicMock, removed: bool
) -> None:
    service.remove_product.return_value = removed

    await bot.press("pdel:5", **_PRESS["inaccessible"])

    assert bot.edits == []
    assert len(bot.sent) == 1
    assert bot.sent[0].chat_id == _CHAT
    assert _rows(bot.sent[0]) == [_CARD_NAV]
    assert len(bot.answers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("removed", "popup"),
    [
        (True, texts.LIST_PRODUCT_REMOVED_TEXT),
        (False, texts.PRODUCT_ALREADY_GONE_TEXT),
    ],
)
async def test_remove_product_without_any_message_pops_up_the_outcome(
    bot: _Bot, service: MagicMock, removed: bool, popup: str
) -> None:
    service.remove_product.return_value = removed

    await bot.press("pdel:5", **_PRESS["none"])

    assert [(a.text, a.show_alert) for a in bot.answers] == [(popup, True)]
    assert bot.sent == []
    assert bot.edits == []


@pytest.mark.asyncio
async def test_foreign_and_missing_rows_get_the_same_answer_on_every_path(
    real_dispatcher: Dispatcher, service: MagicMock
) -> None:
    """A foreign row looks like a missing one (``None``/``False``/``not_found``)
    to the handlers, so the user sees the same words for card, alert, product."""
    seen: list[tuple[str, str | None]] = []
    for data in ("pcard:5", "adel:5:5", "pdel:5"):
        one = _Bot(real_dispatcher)
        await one.press(data)
        seen.append(
            (data, one.answers[0].text if one.answers[0].text else one.edits[0].text)
        )

    assert seen == [
        ("pcard:5", texts.LIST_GONE_ALERT_TEXT),
        ("adel:5:5", texts.PRODUCT_ALREADY_GONE_TEXT),
        ("pdel:5", texts.PRODUCT_ALREADY_GONE_TEXT),
    ]


@pytest.mark.asyncio
async def test_service_calls_are_scoped_to_the_pressing_user_not_the_chat(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_product.return_value = True
    service.remove_alert.return_value = AlertRemoval.removed

    await bot.press("pcard:1")
    await bot.press("adel:2:7")
    await bot.press("pdel:3")

    assert service.get_product_card.await_args_list == [
        call(_USER, 1),
        call(_USER, 7),
    ]
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
_FORGED_ALERT_SUFFIXES = [
    "0:1",
    "1:0",
    "-1:1",
    "1:-1",
    f"{_MAX_ID + 1}:1",
    f"1:{_MAX_ID + 1}",
    "abc:1",
    "1:abc",
    "1:2:3",  # extra field
    "1:",  # missing product_id
    ":1",  # missing alert_id
    "1.5:1",
    "5",  # the old single-id format
]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["pcard", "pdel"])
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
@pytest.mark.parametrize("suffix", _FORGED_ALERT_SUFFIXES)
async def test_forged_remove_alert_callback_never_reaches_the_service(
    bot: _Bot, service: MagicMock, suffix: str
) -> None:
    await bot.press(f"adel:{suffix}")

    _no_service_call(service)
    assert [(a.text, a.show_alert) for a in bot.answers] == [
        (texts.LIST_STALE_BUTTON_ALERT_TEXT, True)
    ]
    assert bot.sent == []
    assert bot.edits == []


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
    await bot.press(f"adel:{_MAX_ID}:{_MAX_ID}")
    await bot.press(f"pdel:{_MAX_ID}")

    assert service.get_product_card.await_args_list == [
        call(_USER, _MAX_ID),
        call(_USER, _MAX_ID),
    ]
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
        ("adel:5:5", "remove_alert"),
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
async def test_card_read_failure_after_a_removal_reaches_the_errors_handler(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_alert.return_value = AlertRemoval.removed
    service.get_product_card.side_effect = RuntimeError("boom")

    await bot.press("adel:5:5")

    assert bot.edits == []
    assert [r.text for r in bot.sent] == [texts.UNEXPECTED_ERROR_TEXT]


@pytest.mark.asyncio
async def test_unknown_removal_outcome_is_an_error_not_a_silent_success(
    bot: _Bot, service: MagicMock
) -> None:
    service.remove_alert.return_value = "vanished"

    await bot.press("adel:5:5")

    assert bot.edits == []
    assert [r.text for r in bot.sent] == [texts.UNEXPECTED_ERROR_TEXT]
