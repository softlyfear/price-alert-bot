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
from curl_cffi.requests import exceptions as curl_errors

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


class _FlakyCurlSession:
    """Raises the planned errors in turn, then answers with the response."""

    def __init__(self, errors: list[Exception], response: _FakeResponse) -> None:
        self._errors = errors
        self._response = response
        self.calls = 0

    async def request(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [curl_errors.ConnectionError("reset"), curl_errors.ProxyError("reset")]
)
async def test_dropped_connection_is_sent_once_more(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    fake = _FlakyCurlSession([error], _FakeResponse())
    session = OzonSession()
    monkeypatch.setattr(session, "_session", fake)

    reply = await session.get(
        "http://x/",
        params={},
        access=make_access(1),
        timeout_seconds=1.0,
        max_body_bytes=10,
    )

    assert reply == OzonReply(200, b"abcd")
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_second_drop_is_raised_after_two_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[Exception] = [
        curl_errors.ConnectionError("reset"),
        curl_errors.ProxyError("reset"),
    ]
    fake = _FlakyCurlSession(errors, _FakeResponse())
    session = OzonSession()
    monkeypatch.setattr(session, "_session", fake)

    with pytest.raises(curl_errors.ProxyError):
        await session.get_redirect("http://x/", timeout_seconds=1.0)

    assert fake.calls == 2


@pytest.mark.asyncio
async def test_timeout_is_not_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FlakyCurlSession([curl_errors.Timeout("slow")], _FakeResponse())
    session = OzonSession()
    monkeypatch.setattr(session, "_session", fake)

    with pytest.raises(curl_errors.Timeout):
        await session.get_redirect("http://x/", timeout_seconds=1.0)

    assert fake.calls == 1
