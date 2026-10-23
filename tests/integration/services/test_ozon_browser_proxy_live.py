"""`fetch_ozon_access` with a proxy: pre-check, relay and Chromium launch (PAB-111 AC3).

The SOCKS5 side is the real stand-in server (`tests/integration/socks_stub.py`),
the relay is the real `SocksRelay`; only Playwright is the hand-written fake of
`tests/unit/services/test_ozon_browser.py` (no Chromium here: a real one is in
`test_ozon_proxy_chromium_live.py`). The fake `launch` plays the part of the
browser: it looks at the stand-in and at the relay at the moment of launch.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Error as PlaywrightError

from app.services.ozon_access import OzonAccessFetchError
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_browser import fetch_ozon_access
from app.services.socks_relay import SocksError
from app.services.socks_relay import SocksProxy
from tests.integration.socks_stub import PASSWORD
from tests.integration.socks_stub import USERNAME
from tests.integration.socks_stub import SocksStub
from tests.unit.services.test_ozon_browser import _cookie
from tests.unit.services.test_ozon_browser import _Fake
from tests.unit.services.test_ozon_browser import install

_FUSE = 15.0
_HOME = "https://shop.example.test/"


class _Observed:
    """What the fake browser saw at the moment of launch."""

    def __init__(self) -> None:
        self.precheck_connections_closed = False
        self.precheck_seen: list[tuple[int | None, str | None, int | None]] = []
        self.relay_answer = b""
        self.relay_endpoint: tuple[str, int] | None = None


def _hook_launch(fake: _Fake, stub: SocksStub, observed: _Observed) -> None:
    """Make `launch` look at the stand-in, then open a tunnel via the relay."""
    original = fake.chromium.launch

    async def launch(**kwargs: Any) -> Any:
        observed.precheck_connections_closed = await asyncio.to_thread(
            stub.wait_all_closed
        )
        observed.precheck_seen = [(c.atyp, c.host, c.port) for c in stub.connections]
        server = urlsplit(kwargs["proxy"]["server"])
        assert server.hostname is not None
        assert server.port is not None
        observed.relay_endpoint = (server.hostname, server.port)
        reader, writer = await asyncio.open_connection(server.hostname, server.port)
        writer.write(b"CONNECT shop.example.test:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        observed.relay_answer = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), _FUSE
        )
        writer.close()
        return await original(**kwargs)

    fake.chromium.launch = launch  # type: ignore[method-assign]


def _proxy(stub: SocksStub, *, user: str = USERNAME, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return SocksProxy.from_url(f"socks5://{user}:{password}@127.0.0.1:{stub.port}")


async def _assert_refused(endpoint: tuple[str, int]) -> None:
    with pytest.raises(OSError):
        await asyncio.wait_for(asyncio.open_connection(*endpoint), _FUSE)


@pytest.mark.asyncio
async def test_precheck_precedes_the_launch_and_the_browser_uses_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, _Fake())
    observed = _Observed()
    with SocksStub() as stub:
        _hook_launch(fake, stub, observed)

        access = await asyncio.wait_for(
            fetch_ozon_access(
                sandbox=True,
                home_url=_HOME,
                cookie_domain="ozon.ru",
                proxy=_proxy(stub),
            ),
            _FUSE,
        )

    assert access.user_agent  # the acquisition completed
    # At the launch only the pre-check had connected, and it was already closed.
    assert observed.precheck_seen == [(3, "shop.example.test", 443)]
    assert observed.precheck_connections_closed is True
    # The browser got the relay, not the SOCKS5 proxy itself.
    assert observed.relay_endpoint is not None
    assert fake.launch_kwargs["proxy"] == {
        "server": f"http://127.0.0.1:{observed.relay_endpoint[1]}"
    }
    assert observed.relay_answer == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    # The tunnel the "browser" opened reached the proxy with the credentials.
    assert len(stub.connections) == 2
    tunnel = stub.connections[1]
    assert (tunnel.username, tunnel.password) == (USERNAME, PASSWORD)
    assert (tunnel.atyp, tunnel.host, tunnel.port) == (3, "shop.example.test", 443)
    # The relay lived exactly as long as the browser.
    await _assert_refused(observed.relay_endpoint)
    assert fake.browser is not None
    assert fake.browser.closed == 1


@pytest.mark.asyncio
async def test_without_a_proxy_nothing_is_launched_through_a_relay_or_prechecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, _Fake())
    listening_before = _listening_loopback_ports()

    await asyncio.wait_for(fetch_ozon_access(sandbox=True), _FUSE)

    assert fake.launch_kwargs["proxy"] is None
    assert _listening_loopback_ports() == listening_before


def _listening_loopback_ports() -> set[int]:
    """Listening TCP ports on 127.0.0.1 (Linux `/proc/net/tcp`)."""
    ports: set[int] = set()
    with open("/proc/net/tcp") as table:
        next(table)
        for row in table:
            fields = row.split()
            address, port = fields[1].split(":")
            if fields[3] == "0A" and address == "0100007F":
                ports.add(int(port, 16))
    return ports


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "detail"),
    [
        ("reject_auth", "proxy_auth_failed"),
        ("reject_method", "proxy_auth_failed"),
        ("reject_connect", "proxy_connect_rejected"),
        ("bad_version", "proxy_protocol_error"),
        ("close", "proxy_protocol_error"),
    ],
)
async def test_refused_precheck_is_proxy_failed_and_the_browser_is_never_launched(
    monkeypatch: pytest.MonkeyPatch, mode: str, detail: str
) -> None:
    fake = install(monkeypatch, _Fake())
    listening_before = _listening_loopback_ports() - {0}
    with SocksStub(mode=mode, username="LOGIN-MARKER", password="PASS-MARKER") as stub:
        listening_with_stub = _listening_loopback_ports()
        with pytest.raises(OzonAccessFetchError) as caught:
            await asyncio.wait_for(
                fetch_ozon_access(
                    sandbox=True,
                    home_url=_HOME,
                    proxy=_proxy(stub, user="LOGIN-MARKER", password="PASS-MARKER"),
                ),
                _FUSE,
            )
        listening_after = _listening_loopback_ports()

    failure = caught.value
    assert (failure.stage, failure.detail) == (OzonAccessStage.proxy_failed, detail)
    assert isinstance(failure.__cause__, SocksError)
    assert fake.launch_kwargs == {}  # Chromium was not started
    assert fake.browser is None
    for text in (str(failure), repr(failure), failure.detail):
        assert "LOGIN-MARKER" not in text
        assert "PASS-MARKER" not in text
    # no relay was opened for a refused proxy
    assert listening_after == listening_with_stub
    assert stub.port in listening_with_stub
    assert stub.port not in listening_before


@pytest.mark.asyncio
async def test_unreachable_proxy_is_proxy_failed_and_the_browser_is_never_launched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, _Fake())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    with pytest.raises(OzonAccessFetchError) as caught:
        await asyncio.wait_for(
            fetch_ozon_access(
                sandbox=True,
                home_url=_HOME,
                proxy=SocksProxy.from_url(f"socks5://u:p@127.0.0.1:{port}"),
            ),
            _FUSE,
        )

    assert (caught.value.stage, caught.value.detail) == (
        OzonAccessStage.proxy_failed,
        "proxy_unreachable",
    )
    assert fake.launch_kwargs == {}


@pytest.mark.asyncio
async def test_cancelled_acquisition_closes_the_relay_and_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline of the provider cancels the fetch while it waits for the
    cookie: the relay (alive meanwhile - positive control) is closed with the
    browser."""
    fake = install(monkeypatch, _Fake([[_cookie("__Secure-ETC", "e")]]))
    observed = _Observed()
    with SocksStub() as stub:
        _hook_launch(fake, stub, observed)
        task = asyncio.ensure_future(
            fetch_ozon_access(sandbox=True, home_url=_HOME, proxy=_proxy(stub))
        )
        await asyncio.wait_for(fake.cookies_read.wait(), _FUSE)
        assert observed.relay_endpoint is not None
        _reader, writer = await asyncio.open_connection(*observed.relay_endpoint)
        writer.close()  # positive control: the relay accepts while the browser lives

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _FUSE)
        await _assert_refused(observed.relay_endpoint)
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    assert fake.browser is not None
    assert fake.browser.closed == 1


@pytest.mark.asyncio
async def test_launch_failure_with_a_proxy_is_launch_failed_and_closes_the_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch, _Fake(launch_raises=PlaywrightError("BrowserType.launch: boom"))
    )
    seen_relay: list[tuple[str, int]] = []
    with SocksStub() as stub:
        original = fake.chromium.launch

        async def launch(**kwargs: Any) -> Any:
            server = urlsplit(kwargs["proxy"]["server"])
            assert server.hostname is not None
            assert server.port is not None
            seen_relay.append((server.hostname, server.port))
            return await original(**kwargs)

        fake.chromium.launch = launch  # type: ignore[method-assign]

        with pytest.raises(OzonAccessFetchError) as caught:
            await asyncio.wait_for(
                fetch_ozon_access(sandbox=True, home_url=_HOME, proxy=_proxy(stub)),
                _FUSE,
            )

    assert caught.value.stage is OzonAccessStage.launch_failed
    assert len(seen_relay) == 1
    await _assert_refused(seen_relay[0])


@pytest.mark.asyncio
async def test_browser_failure_after_a_proxy_refusal_is_proxy_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch,
        _Fake(
            goto_raises=PlaywrightError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")
        ),
    )
    with SocksStub() as stub:
        original = fake.chromium.launch

        async def launch(**kwargs: Any) -> Any:
            # The pre-check has passed; now the proxy refuses the browser's tunnel.
            stub.mode = "reject_connect"
            server = urlsplit(kwargs["proxy"]["server"])
            assert server.hostname is not None
            assert server.port is not None
            reader, writer = await asyncio.open_connection(server.hostname, server.port)
            writer.write(b"CONNECT shop.example.test:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.read(), _FUSE)
            writer.close()
            return await original(**kwargs)

        fake.chromium.launch = launch  # type: ignore[method-assign]

        with pytest.raises(OzonAccessFetchError) as caught:
            await asyncio.wait_for(
                fetch_ozon_access(sandbox=True, home_url=_HOME, proxy=_proxy(stub)),
                _FUSE,
            )

    assert caught.value.stage is OzonAccessStage.proxy_failed
    assert caught.value.detail == "proxy_connect_rejected"
    assert isinstance(caught.value.__cause__, PlaywrightError)
