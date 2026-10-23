"""The real `curl_cffi` transport of `OzonSession` against a local server (PAB-110 AC8).

Properties proved by observation of the server side, not by reading the code:
`Location` is not followed, the body of a non-2xx response is not downloaded,
a cookie set by a response does not come back, dead proxy variables in the
environment do not matter, cancellation reaches the caller and leaves the
session usable. Nothing here talks to Ozon.
"""

from __future__ import annotations

import asyncio
import socket
import urllib.error
import urllib.request
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from curl_cffi.requests import AsyncSession

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonReply
from app.services.ozon_client import OzonSession
from tests.integration.ozon_stub import BIG_BODY_BYTES
from tests.integration.ozon_stub import OzonAntibotStub
from tests.unit.ozon_stubs import StubOzonAccess
from tests.unit.ozon_stubs import make_access

_FUSE = 30.0
_CAP = OzonClient.MAX_BODY_BYTES


async def _get(
    session: OzonSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    deadline: float = 10.0,
    cap: int = _CAP,
) -> OzonReply:
    return await asyncio.wait_for(
        session.get(
            url,
            params=params or {},
            access=make_access(1),
            timeout_seconds=deadline,
            max_body_bytes=cap,
        ),
        _FUSE,
    )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[OzonSession]:
    created = OzonSession()
    try:
        yield created
    finally:
        await created.aclose()


@pytest.mark.asyncio
async def test_location_of_a_redirect_is_not_followed(session: OzonSession) -> None:
    with OzonAntibotStub() as stub:
        reply = await _get(session, stub.base_url + "/redir")

    assert reply == OzonReply(307, None)
    assert stub.trap_hits == 0


@pytest.mark.asyncio
async def test_a_followed_redirect_would_hit_the_trap_positive_control() -> None:
    """The control of the test above: a client that follows redirects does reach
    `/trap`, so a zero there means something."""
    with OzonAntibotStub() as stub:
        async with AsyncSession() as plain:
            response = await plain.get(stub.base_url + "/redir", allow_redirects=True)

    assert response.status_code == 200
    assert stub.trap_hits == 1


@pytest.mark.asyncio
async def test_body_of_a_non_2xx_response_is_not_downloaded(
    session: OzonSession,
) -> None:
    """The stand-in answers `403` with a 16 MiB body and counts the bytes that
    left it: a client that reads the body takes all of them (control below);
    this one stops far short of that (socket buffers take the first part)."""
    with OzonAntibotStub() as stub:
        reply = await _get(session, stub.base_url + "/big", params={"status": "403"})
        await asyncio.sleep(0.5)  # let the server notice the closed connection
        sent = stub.big_bytes_sent

    assert reply == OzonReply(403, None)
    assert sent < BIG_BODY_BYTES // 2


@pytest.mark.asyncio
async def test_a_reader_takes_the_whole_big_body_positive_control() -> None:
    with OzonAntibotStub() as stub:
        url = stub.base_url + "/big?status=403"
        body = await asyncio.to_thread(_read_all, url)
        sent = stub.big_bytes_sent

    assert len(body) == BIG_BODY_BYTES
    assert sent == BIG_BODY_BYTES


def _read_all(url: str) -> bytes:
    request = urllib.request.Request(url)  # noqa: S310 - loopback stub
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return bytes(response.read())
    except urllib.error.HTTPError as e:
        return bytes(e.read())


@pytest.mark.asyncio
async def test_cookie_set_by_a_response_does_not_come_back(
    session: OzonSession,
) -> None:
    with OzonAntibotStub() as stub:
        setter = await _get(session, stub.base_url + "/setcookie", cap=_CAP)
        await _get(session, stub.base_url + "/echo")

    assert setter == OzonReply(200, b"{}")
    assert stub.echo_requests[0]["cookie"] == (
        "__Secure-access-token=OZON-TOKEN-MARKER-1; abt_data=OZON-ABT-MARKER-1"
    )
    assert "leak" not in stub.echo_requests[0]["cookie"]


@pytest.mark.asyncio
async def test_a_jar_session_would_return_the_cookie_positive_control() -> None:
    with OzonAntibotStub() as stub:
        async with AsyncSession() as plain:
            await plain.get(stub.base_url + "/setcookie")
            await plain.get(stub.base_url + "/echo")

    assert "leak=LEAK-MARKER" in stub.echo_requests[0]["cookie"]


@pytest.mark.asyncio
async def test_request_carries_the_access_cookies_and_its_user_agent(
    session: OzonSession,
) -> None:
    with OzonAntibotStub() as stub:
        await _get(session, stub.base_url + "/echo")

    assert stub.echo_requests[0]["user_agent"] == "Mozilla/5.0 UA-MARKER-1"
    assert stub.echo_requests[0]["cookie"].startswith("__Secure-access-token=")


def _closed_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _dead_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    dead = f"http://127.0.0.1:{_closed_local_port()}"
    for name in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.setenv(name, dead)
        monkeypatch.setenv(name.lower(), dead)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_dead_proxy_variables_do_not_stop_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dead_proxy_environment(monkeypatch)
    session = OzonSession()
    try:
        with OzonAntibotStub() as stub:
            reply = await _get(session, stub.base_url + "/echo")
    finally:
        await session.aclose()

    assert reply.status == 200
    assert len(stub.echo_requests) == 1


@pytest.mark.asyncio
async def test_dead_proxy_variables_do_stop_a_default_session_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dead_proxy_environment(monkeypatch)
    with OzonAntibotStub() as stub:
        async with AsyncSession() as plain:
            with pytest.raises(Exception, match=r".+"):
                await plain.get(stub.base_url + "/echo", timeout=5)

    assert stub.echo_requests == []


@pytest.mark.asyncio
async def test_cancellation_reaches_the_caller_and_the_session_stays_usable(
    session: OzonSession,
) -> None:
    with OzonAntibotStub() as stub:
        task = asyncio.ensure_future(
            session.get(
                stub.base_url + "/slow",
                params={},
                access=make_access(1),
                timeout_seconds=30.0,
                max_body_bytes=_CAP,
            )
        )
        started = await asyncio.to_thread(stub.slow_started.wait, _FUSE)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _FUSE)
        stub.release_slow.set()
        after = await _get(session, stub.base_url + "/echo")

    assert started
    assert after.status == 200


# --- body cap and failures through the client --------------------------------


async def _client_fetch(
    monkeypatch: pytest.MonkeyPatch, stub: OzonAntibotStub, path: str
) -> object:
    monkeypatch.setattr(OzonClient, "URL", stub.base_url + path)
    session = OzonSession()
    try:
        client = OzonClient(session, StubOzonAccess())
        return await asyncio.wait_for(client.get_product_data(3593896354), _FUSE)
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_body_of_exactly_the_cap_still_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with OzonAntibotStub() as stub:
        result = await _client_fetch(monkeypatch, stub, f"/body?size={_CAP}")

    assert result == MarketplaceProductData(
        name="Лонгслив тельняшка длинный рукав 1 шт", price=49500, card_price=47100
    )


@pytest.mark.asyncio
async def test_oversized_2xx_body_is_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with OzonAntibotStub() as stub:
        result = await _client_fetch(monkeypatch, stub, f"/body?size={_CAP + 1}")

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="body exceeds 4194304 bytes"
    )


@pytest.mark.asyncio
async def test_connection_refused_is_transport_error_named_by_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        OzonClient, "URL", f"http://127.0.0.1:{_closed_local_port()}/api"
    )
    session = OzonSession()
    try:
        result = await OzonClient(session, StubOzonAccess()).get_product_data(1)
    finally:
        await session.aclose()

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.transport_error
    assert result.detail == "ConnectionError"


@pytest.mark.asyncio
async def test_a_stalled_server_ends_as_transport_error_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OzonClient, "TIMEOUT_SECONDS", 0.5)
    with OzonAntibotStub() as stub:
        result = await _client_fetch(monkeypatch, stub, "/slow")

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.transport_error
    assert result.detail in {"TimeoutError", "Timeout", "ReadTimeout"}
