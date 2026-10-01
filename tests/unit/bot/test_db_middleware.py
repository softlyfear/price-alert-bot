"""Tests for app.bot.middlewares.db.DbSessionMiddleware (PAB-068 AC5).

The session factory is a fake whose sessions record ``commit``/``rollback``/
``close`` in one shared event list, so both the outcome and the order of the
transaction boundary are observable without a database.
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any
from typing import cast

import pytest
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.middlewares.db import DbSessionMiddleware


class _FakeSession:
    def __init__(self, events: list[str], *, commit_error: Exception | None) -> None:
        self._events = events
        self._commit_error = commit_error

    async def __aenter__(self) -> _FakeSession:
        self._events.append("open")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._events.append("close")

    async def commit(self) -> None:
        self._events.append("commit")
        if self._commit_error is not None:
            raise self._commit_error

    async def rollback(self) -> None:
        self._events.append("rollback")


class _Factory:
    def __init__(self, *, commit_error: Exception | None = None) -> None:
        self.events: list[str] = []
        self.sessions: list[_FakeSession] = []
        self._commit_error = commit_error

    def __call__(self) -> _FakeSession:
        session = _FakeSession(self.events, commit_error=self._commit_error)
        self.sessions.append(session)
        return session

    def as_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return cast("async_sessionmaker[AsyncSession]", self)


_EVENT = Update(update_id=1)


@pytest.mark.asyncio
async def test_success_passes_session_commits_once_then_closes() -> None:
    factory = _Factory()
    middleware = DbSessionMiddleware(factory.as_sessionmaker())
    seen: list[Any] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        seen.append(data["session"])
        factory.events.append("handler")
        return "handler-result"

    data: dict[str, Any] = {}
    result = await middleware(handler, _EVENT, data)

    assert result == "handler-result"
    assert seen == [factory.sessions[0]]
    assert data["session"] is factory.sessions[0]
    assert factory.events == ["open", "handler", "commit", "close"]


@pytest.mark.asyncio
async def test_handler_error_rolls_back_re_raises_same_object_and_closes() -> None:
    factory = _Factory()
    middleware = DbSessionMiddleware(factory.as_sessionmaker())
    boom = RuntimeError("handler boom")

    async def handler(event: Any, data: dict[str, Any]) -> None:
        raise boom

    with pytest.raises(RuntimeError) as excinfo:
        await middleware(handler, _EVENT, {})

    assert excinfo.value is boom
    assert factory.events == ["open", "rollback", "close"]


@pytest.mark.asyncio
async def test_each_update_gets_its_own_session() -> None:
    factory = _Factory()
    middleware = DbSessionMiddleware(factory.as_sessionmaker())

    async def handler(event: Any, data: dict[str, Any]) -> None:
        return None

    await middleware(handler, _EVENT, {})
    await middleware(handler, _EVENT, {})

    assert len(factory.sessions) == 2
    assert factory.sessions[0] is not factory.sessions[1]
    assert factory.events == ["open", "commit", "close", "open", "commit", "close"]


@pytest.mark.asyncio
async def test_commit_failure_propagates_and_session_is_still_closed() -> None:
    commit_error = RuntimeError("commit boom")
    factory = _Factory(commit_error=commit_error)
    middleware = DbSessionMiddleware(factory.as_sessionmaker())

    async def handler(event: Any, data: dict[str, Any]) -> None:
        return None

    with pytest.raises(RuntimeError) as excinfo:
        await middleware(handler, _EVENT, {})

    assert excinfo.value is commit_error
    assert factory.events == ["open", "commit", "close"]


@pytest.mark.asyncio
async def test_cancellation_does_not_commit_and_session_is_closed() -> None:
    factory = _Factory()
    middleware = DbSessionMiddleware(factory.as_sessionmaker())

    async def handler(event: Any, data: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await middleware(handler, _EVENT, {})

    assert "commit" not in factory.events
    assert factory.events[-1] == "close"
