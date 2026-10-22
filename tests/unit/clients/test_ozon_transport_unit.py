"""`OzonSession.get` on a fake `curl_cffi` session (the branch live cannot reach).

A real streamed response always has an abort event; the transport still
tolerates `quit_now is None` (nothing to abort) and must close the response.
The fake stands in for the `curl_cffi` `AsyncSession`, the boundary of the
object under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.services.ozon_client import OzonReply
from app.services.ozon_client import OzonSession
from tests.unit.ozon_stubs import make_access


class _FakeResponse:
    status_code = 200
    quit_now = None

    def __init__(self) -> None:
        self.closed = 0

    async def aiter_content(self) -> AsyncIterator[bytes]:
        yield b"ab"
        yield b"cd"

    async def aclose(self) -> None:
        self.closed += 1


class _FakeCurlSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def request(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        return self._response

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_response_without_an_abort_event_is_read_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse()
    session = OzonSession()
    monkeypatch.setattr(session, "_session", _FakeCurlSession(response))

    reply = await session.get(
        "http://x/",
        params={},
        access=make_access(1),
        timeout_seconds=1.0,
        max_body_bytes=10,
    )

    assert reply == OzonReply(200, b"abcd")
    assert response.closed == 1
