"""Tests for app.handlers.fallback, free text outside any dialog (PAB-094).

Everything goes through ``Dispatcher.feed_update`` on the real
``create_dispatcher()`` output. The replaced boundary is ``TrackingService``:
the router under test must never touch it, which is asserted as "no call at
all" rather than by looking at how the router is built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

import app.handlers.add_product as add_product_module
import app.handlers.fallback as fallback_module
from app.bot import texts
from app.bot.states import AddProduct
from app.schemas.marketplace import MarketplaceProductData
from app.services.tracking import TrackingService
from tests.unit.conftest import FAKE_BOT_USER
from tests.unit.conftest import make_message_update
from tests.unit.handlers.add_dialog_support import _CHAT
from tests.unit.handlers.add_dialog_support import _CONFIRMING_DATA
from tests.unit.handlers.add_dialog_support import _USER
from tests.unit.handlers.add_dialog_support import _BoundSession
from tests.unit.handlers.add_dialog_support import _Dialog
from tests.unit.handlers.add_dialog_support import _Faults
from tests.unit.handlers.add_dialog_support import _responder
from tests.unit.handlers.card_dialog_support import page_of

if TYPE_CHECKING:
    from loguru import Record

_MENU_ROWS = [
    [("➕ Добавить товар", "menu:add")],
    [("Мои товары", "menu:list"), ("Помощь", "menu:help")],
]


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(spec=TrackingService)
    mock.list_page.return_value = page_of([])
    mock.find_tracked_product.return_value = None
    mock.product_limit.return_value = 50
    mock.preview.return_value = MarketplaceProductData(name="Товар", price=100_00)
    monkeypatch.setattr(add_product_module, "TrackingService", lambda **_kw: mock)
    return mock


@pytest.fixture
def dialog(real_dispatcher: Dispatcher, service: MagicMock) -> _Dialog:
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(real_dispatcher, bot, faults)


def _rows(request: SendMessage | EditMessageText) -> list[list[tuple[str, str | None]]]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def _plain_texts(dialog: _Dialog) -> list[str]:
    return [m.text for m in dialog.sent]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "title", "article", "callback"),
    [
        pytest.param(
            "https://www.wildberries.ru/catalog/12345678/detail.aspx",
            "Wildberries",
            12345678,
            "addref:wb:12345678",
            id="wildberries-link",
        ),
        pytest.param(
            "https://www.ozon.ru/product/krossovki-muzhskie-1234567/",
            "Ozon",
            1234567,
            "addref:ozon:1234567",
            id="ozon-link",
        ),
    ],
)
async def test_recognised_reference_gets_one_offer_with_the_add_button(
    dialog: _Dialog,
    service: MagicMock,
    text: str,
    title: str,
    article: int,
    callback: str,
) -> None:
    await dialog.say(text)

    assert len(dialog.requests) == 1
    offer = dialog.sent[0]
    assert offer.text == f"Добавить этот товар? {title}, артикул {article}."
    assert offer.parse_mode is None
    assert _rows(offer) == [
        [("➕ Добавить этот товар", callback)],
        [("Главное меню", "menu:start")],
    ]
    assert await dialog.state() is None
    assert service.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update_kwargs",
    [
        pytest.param({"text": "привет, как дела"}, id="garbage-text"),
        pytest.param({"text": "/foo"}, id="unknown-command"),
        pytest.param({"photo": True}, id="photo"),
        pytest.param({"text": "https://evil.com/catalog/12345678/detail.aspx"}, id="x"),
    ],
)
async def test_unrecognised_message_gets_one_hint_with_the_main_menu(
    dialog: _Dialog, service: MagicMock, update_kwargs: dict[str, Any]
) -> None:
    await dialog.feed(
        make_message_update(update_id=1, chat_id=9_999, user_id=_USER, **update_kwargs)
    )

    assert len(dialog.requests) == 1
    hint = dialog.sent[0]
    assert hint.text == texts.FALLBACK_TEXT
    assert _rows(hint) == _MENU_ROWS
    assert await dialog.state() is None
    assert service.mock_calls == []


@pytest.mark.asyncio
async def test_message_without_a_sender_is_ignored_silently(
    dialog: _Dialog, service: MagicMock
) -> None:
    await dialog.feed(make_message_update(text="привет", user_id=None))
    await dialog.feed(make_message_update(text="12345678", user_id=None, update_id=2))

    assert dialog.requests == []
    assert service.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("start", texts.START_TEXT),
        ("help", texts.help_text(50)),
        ("cancel", texts.NOTHING_TO_CANCEL_TEXT),
        ("add", texts.ADD_PROMPT_LINK_TEXT),
        ("list", texts.LIST_EMPTY_TEXT),
    ],
)
async def test_each_known_command_is_answered_by_its_own_handler(
    dialog: _Dialog, command: str, expected: str
) -> None:
    await dialog.command(command)

    assert _plain_texts(dialog) == [expected]


@pytest.mark.asyncio
async def test_link_inside_waiting_link_is_checked_and_gets_no_fallback_or_offer(
    dialog: _Dialog, service: MagicMock
) -> None:
    await dialog.command("add")

    await dialog.say("https://www.wildberries.ru/catalog/12345678/detail.aspx")

    service.preview.assert_awaited_once()
    shown = [
        r.text for r in dialog.requests if isinstance(r, (SendMessage, EditMessageText))
    ]
    assert texts.FALLBACK_TEXT not in shown
    assert not any(t.startswith("Добавить этот товар?") for t in shown)
    assert await dialog.state() == AddProduct.waiting_target_price.state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "data", "text", "expected"),
    [
        pytest.param(
            AddProduct.waiting_link.state,
            {},
            "привет",
            texts.ADD_LINK_FORMAT_HINT_TEXT,
            id="waiting-link",
        ),
        pytest.param(
            AddProduct.waiting_target_price.state,
            dict(_CONFIRMING_DATA),
            "abc",
            texts.ADD_PRICE_INVALID_TEXT,
            id="waiting-target-price",
        ),
        pytest.param(
            AddProduct.confirming.state,
            dict(_CONFIRMING_DATA),
            "привет",
            texts.ADD_CONFIRM_HINT_TEXT,
            id="confirming",
        ),
    ],
)
async def test_text_inside_a_dialog_state_is_answered_by_the_dialog_only(
    dialog: _Dialog,
    service: MagicMock,
    state: str,
    data: dict[str, Any],
    text: str,
    expected: str,
) -> None:
    await dialog.set_state(state, data)

    await dialog.say(text)

    assert _plain_texts(dialog) == [expected]
    assert await dialog.state() == state
    assert service.mock_calls == []


@pytest.mark.asyncio
async def test_fallback_logs_nothing_about_the_user_text(
    dialog: _Dialog, log_records: list[Record]
) -> None:
    marker = "USER-TEXT-MARKER-5512"
    await dialog.say(marker)
    await dialog.say("https://www.ozon.ru/product/" + marker + "-1234567/")

    assert len(dialog.sent) == 2  # positive control: both messages were handled
    assert [
        r for r in log_records if marker in str(r["message"]) + str(r["extra"])
    ] == []


_CHAT_PRIVATE = 9_999
_CHAT_GROUP = -100_500
_LINK_TEXT = "https://www.wildberries.ru/catalog/12345678/detail.aspx"
_FILE: dict[str, Any] = {"file_id": "f", "file_unique_id": "u"}
_LOC: dict[str, Any] = {"latitude": 1.0, "longitude": 1.0}
_SIZED: dict[str, Any] = {**_FILE, "width": 1, "height": 1, "duration": 1}
_STICKER: dict[str, Any] = {
    "sticker": {
        **_FILE,
        "type": "regular",
        "width": 1,
        "height": 1,
        "is_animated": False,
        "is_video": False,
    }
}
_PIN_CHAT: dict[str, Any] = {"id": 1, "type": "private"}
_STORY: dict[str, Any] = {"story": {"chat": _PIN_CHAT, "id": 1}}
_GAME: dict[str, Any] = {
    "game": {
        "title": "t",
        "description": "d",
        "photo": [{**_FILE, "width": 1, "height": 1}],
    }
}
_CHECKLIST: dict[str, Any] = {
    "checklist": {"title": "t", "tasks": [{"id": 1, "text": "a"}]}
}
_PAID_MEDIA: dict[str, Any] = {
    "paid_media": {"star_count": 1, "paid_media": [{"type": "preview"}]}
}
_GIVEAWAY: dict[str, Any] = {
    "giveaway": {
        "chats": [_PIN_CHAT],
        "winners_selection_date": 1,
        "winner_count": 1,
    }
}
_USER_DICT: dict[str, Any] = {"id": 77, "is_bot": False, "first_name": "Other"}


def _update(
    chat_type: str, extra: dict[str, Any] | None = None, text: str | None = None
) -> Any:
    chat_id = _CHAT_PRIVATE if chat_type == "private" else _CHAT_GROUP
    return make_message_update(
        text=text,
        update_id=1,
        chat_id=chat_id,
        user_id=_USER,
        chat_type=chat_type,
        extra=extra,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
@pytest.mark.parametrize(
    ("text", "extra"),
    [
        pytest.param(_LINK_TEXT, None, id="link"),
        pytest.param("привет", None, id="text"),
        pytest.param("/foo", None, id="unknown-command"),
        pytest.param(None, _STICKER, id="sticker"),
        pytest.param(None, _STORY, id="story"),
        pytest.param(None, _GAME, id="game"),
        pytest.param(None, _CHECKLIST, id="checklist"),
        pytest.param(None, _PAID_MEDIA, id="paid-media"),
        pytest.param(None, _GIVEAWAY, id="giveaway"),
    ],
)
async def test_non_private_chat_gets_no_reply_while_private_chat_does(
    dialog: _Dialog,
    service: MagicMock,
    chat_type: str,
    text: str | None,
    extra: dict[str, Any] | None,
) -> None:
    await dialog.feed(_update(chat_type, extra, text))

    assert dialog.requests == []

    await dialog.feed(_update("private", extra, text))  # positive control

    assert len(dialog.requests) == 1
    assert service.mock_calls == []


_WHITELIST: list[Any] = [
    pytest.param(None, "привет", id="text"),
    pytest.param(_STICKER, None, id="sticker"),
    pytest.param({"photo": [{**_FILE, "width": 1, "height": 1}]}, None, id="photo"),
    pytest.param({"video": _SIZED}, None, id="video"),
    pytest.param({"animation": _SIZED}, None, id="animation"),
    pytest.param({"document": _FILE}, None, id="document"),
    pytest.param({"audio": {**_FILE, "duration": 1}}, None, id="audio"),
    pytest.param({"voice": {**_FILE, "duration": 1}}, None, id="voice"),
    pytest.param(
        {"video_note": {**_FILE, "length": 1, "duration": 1}}, None, id="video-note"
    ),
    pytest.param({"location": _LOC}, None, id="location"),
    pytest.param(
        {"venue": {"location": _LOC, "title": "t", "address": "a"}}, None, id="venue"
    ),
    pytest.param(
        {"contact": {"phone_number": "1", "first_name": "a"}}, None, id="contact"
    ),
    pytest.param(
        {
            "poll": {
                "id": "1",
                "question": "q",
                "options": [{"text": "a", "voter_count": 0}],
                "total_voter_count": 0,
                "is_closed": False,
                "is_anonymous": True,
                "type": "regular",
                "allows_multiple_answers": False,
            }
        },
        None,
        id="poll",
    ),
    pytest.param({"dice": {"emoji": "\U0001f3b2", "value": 1}}, None, id="dice"),
    pytest.param(_STORY, None, id="story"),
    pytest.param(_GAME, None, id="game"),
    pytest.param(_CHECKLIST, None, id="checklist"),
    pytest.param(_PAID_MEDIA, None, id="paid-media"),
    pytest.param(_GIVEAWAY, None, id="giveaway"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("extra", "text"), _WHITELIST)
async def test_private_chat_answers_every_whitelisted_content_type(
    dialog: _Dialog, extra: dict[str, Any] | None, text: str | None
) -> None:
    await dialog.feed(_update("private", extra, text))

    assert [m.text for m in dialog.sent] == [texts.FALLBACK_TEXT]
    assert len(dialog.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["private", "group"])
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(
            {
                "pinned_message": {
                    "message_id": 2,
                    "date": 1,
                    "chat": {"id": 1, "type": "private"},
                }
            },
            id="pinned-message",
        ),
        pytest.param(
            {"message_auto_delete_timer_changed": {"message_auto_delete_time": 60}},
            id="auto-delete-timer",
        ),
        pytest.param({"new_chat_members": [_USER_DICT]}, id="new-chat-members"),
        pytest.param({"left_chat_member": _USER_DICT}, id="left-chat-member"),
    ],
)
async def test_service_message_is_skipped_silently_while_a_sticker_is_answered(
    dialog: _Dialog, chat_type: str, extra: dict[str, Any]
) -> None:
    await dialog.feed(_update(chat_type, extra))

    assert dialog.requests == []

    await dialog.feed(_update("private", _STICKER))  # positive control

    assert [m.text for m in dialog.sent] == [texts.FALLBACK_TEXT]


@pytest.fixture
def fallback_only() -> _Dialog:
    """A dispatcher with just the ``fallback`` router: no dialog handlers exist."""
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(fallback_module.create_router())
    faults = _Faults()
    bot = Bot(token="1:test-token", session=_BoundSession(_responder(faults)))
    bot._me = FAKE_BOT_USER
    return _Dialog(dispatcher, bot, faults)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        AddProduct.waiting_link.state,
        AddProduct.waiting_target_price.state,
        AddProduct.confirming.state,
    ],
)
@pytest.mark.parametrize(
    ("text", "extra"),
    [
        pytest.param(_LINK_TEXT, None, id="link"),
        pytest.param("/foo", None, id="unknown-command"),
        pytest.param(None, _STICKER, id="sticker"),
    ],
)
async def test_fallback_stays_silent_in_a_dialog_state_and_answers_in_the_empty_one(
    fallback_only: _Dialog, state: str, text: str | None, extra: dict[str, Any] | None
) -> None:
    dialog = fallback_only
    await dialog.set_state(state)

    await dialog.feed(
        make_message_update(
            text=text, chat_id=_CHAT, user_id=_USER, extra=extra, update_id=1
        )
    )

    assert dialog.requests == []

    await dialog.set_state(None)  # positive control: same message, empty state
    await dialog.feed(
        make_message_update(
            text=text, chat_id=_CHAT, user_id=_USER, extra=extra, update_id=2
        )
    )

    assert len(dialog.sent) == 1
