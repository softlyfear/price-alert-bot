"""Shared test doubles for ``tests/unit/bot/**``, ``tests/unit/handlers/**``
and ``tests/unit/test_main.py``.

Nothing here touches the network: ``FakeTelegramSession`` is a
``aiogram.client.session.base.BaseSession`` stand-in modelled on aiogram's
own ``tests/mocked_bot.py`` (not shipped in the installed package, hence the
local copy), and ``build_fake_bot`` wires a real ``aiogram.Bot`` to it.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram import Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetMe
from aiogram.methods import GetUpdates
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Update
from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.setup import create_dispatcher

FAKE_BOT_USER: User = User(id=1, is_bot=True, first_name="TestBot", username="test_bot")

Responder = Callable[[TelegramMethod[Any]], Awaitable[Any]]


async def _default_responder(method: TelegramMethod[Any]) -> Any:
    """Return a generic, type-plausible result for any Bot API method.

    ``GetUpdates`` gets an empty batch (so a real polling loop stays alive
    instead of crashing on a non-iterable), ``GetMe`` gets the fake bot
    user, everything else gets ``True`` (the shape every boolean-returning
    ``set_my_*``/``send_*`` method expects on success).
    """
    if isinstance(method, GetUpdates):
        return []
    if isinstance(method, GetMe):
        return FAKE_BOT_USER
    return True


class FakeTelegramSession(BaseSession):
    """A ``BaseSession`` that never opens a socket.

    Every call is routed to a caller-supplied ``responder`` coroutine
    instead of an HTTP client. Every dispatched method is recorded in
    ``requests`` in call order, so tests can assert exactly what was sent
    without depending on wire serialization.
    """

    def __init__(self, responder: Responder | None = None) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._responder = responder or _default_responder
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 -- overrides BaseSession's abstract signature
    ) -> TelegramType:
        self.requests.append(method)
        result: TelegramType = await self._responder(method)
        return result

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 -- overrides BaseSession's abstract signature
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        yield b""


def build_fake_bot(
    token: str = "1:test-token",
    *,
    responder: Responder | None = None,
    preset_me: bool = True,
) -> Bot:
    """Build a real ``aiogram.Bot`` wired to a ``FakeTelegramSession``.

    ``preset_me=True`` (default) sets ``bot._me`` directly -- the same
    shortcut aiogram's own ``MockedBot`` test double uses -- so
    ``Dispatcher.start_polling`` never issues a real ``GetMe`` call through
    the fake session. Tests that specifically exercise the ``GetMe`` round
    trip (a session that answers every call with 401, for instance) pass
    ``preset_me=False``.
    """
    bot = Bot(token=token, session=FakeTelegramSession(responder))
    if preset_me:
        bot._me = FAKE_BOT_USER
    return bot


@pytest.fixture
def real_dispatcher() -> Dispatcher:
    """Build a fresh real ``create_dispatcher()`` output for each test.

    ``app.handlers.create_root_router()`` assembles its routers via
    factories, not module-level ``Router`` singletons, so every test gets
    its own ``Dispatcher`` and its own ``MemoryStorage``. The session
    factory is bound to no engine: a session that never executes a statement
    opens no connection, so handlers that do not touch the database work
    without one. The HTTP client is never closed here because nothing is
    ever sent through it.
    """
    return create_dispatcher(
        MemoryStorage(),
        async_sessionmaker(class_=AsyncSession),
        httpx.AsyncClient(),
        50,
    )


_chat_id_counter = itertools.count(10_000)


@pytest.fixture
def unique_chat_id() -> int:
    """A ``chat_id``/``user_id`` pair distinct from every other test's.

    Isolation does not depend on it (each test has its own dispatcher and
    storage); it just keeps identifiers distinguishable in failure output.
    """
    return next(_chat_id_counter)


def make_command_update(
    command: str,
    *,
    update_id: int = 1,
    chat_id: int = 1000,
    user_id: int = 1000,
    message_id: int = 1,
) -> Update:
    """Build an ``Update`` carrying a private-chat ``/command`` message."""
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "text": f"/{command}",
            },
        }
    )


def make_message_update(
    *,
    text: str | None = None,
    photo: bool = False,
    update_id: int = 1,
    chat_id: int = 1000,
    user_id: int | None = 1000,
    message_id: int = 1,
) -> Update:
    """Build an ``Update`` with a plain text (or photo) message.

    ``user_id=None`` omits ``from``, as for messages without a sender.
    """
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": 0,
        "chat": {"id": chat_id, "type": "private"},
    }
    if user_id is not None:
        message["from"] = {"id": user_id, "is_bot": False, "first_name": "Test"}
    if text is not None:
        message["text"] = text
    if photo:
        message["photo"] = [
            {"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}
        ]
    return Update.model_validate({"update_id": update_id, "message": message})


def make_callback_update(
    data: str,
    *,
    update_id: int = 1,
    chat_id: int = 1000,
    user_id: int = 1000,
    accessible: bool = True,
) -> Update:
    """Build an ``Update`` with an inline-button press.

    ``accessible=False`` makes the attached message ``date == 0``, which
    aiogram models as an inaccessible message.
    """
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": 5,
                    "date": 1 if accessible else 0,
                    "chat": {"id": chat_id, "type": "private"},
                },
            },
        }
    )
