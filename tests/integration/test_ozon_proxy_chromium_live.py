"""A real Chromium through the relay to a SOCKS5 stand-in (PAB-111 AC7 b, g).

`fetch_ozon_access` drives a real Chromium (`sandbox=False`) with `proxy`. The
home page is `https://<name>/` with a name that is not loopback (Chromium lets
loopback bypass a proxy) and resolves nowhere, so a connection can only reach
the stand-in through the relay. The stand-in accepts the tunnel and reads: the
bytes after its `0x00` reply tell the browser's connection (a TLS ClientHello
starts with `0x16`) from the pre-check (it closes without a word). A missing
Chromium is a red test, not a skip.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Iterator

import pytest

from app.services.ozon_access import BrowserOzonAccessProvider
from app.services.ozon_access import OzonAccessFetchError
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_access import OzonAccessUnavailable
from app.services.ozon_browser import fetch_ozon_access
from app.services.socks_relay import SocksProxy
from tests.integration.browser_probe import observe_chromium_environments
from tests.integration.browser_probe import processes_started_by_test
from tests.integration.services.test_ozon_browser_proxy_live import (
    _listening_loopback_ports,
)
from tests.integration.socks_stub import PASSWORD
from tests.integration.socks_stub import USERNAME
from tests.integration.socks_stub import SocksConnection
from tests.integration.socks_stub import SocksStub

_FUSE = 60.0
_NAME = "ozon-proxy-probe.example"
_HOME = f"https://{_NAME}/"
_TLS_HANDSHAKE = 0x16


@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A private `TMPDIR`; short on purpose (Chromium's socket path limit)."""
    with tempfile.TemporaryDirectory(prefix="ozc-", dir="/tmp") as directory:
        monkeypatch.setenv("TMPDIR", directory)
        yield directory


def _proxy(stub: SocksStub) -> SocksProxy:
    return SocksProxy.from_url(f"socks5://{USERNAME}:{PASSWORD}@127.0.0.1:{stub.port}")


def _browser_connections(stub: SocksStub) -> list[SocksConnection]:
    """Tunnels the browser opened to `_NAME` (they start with a ClientHello)."""
    return [
        c
        for c in list(stub.connections)
        if c.host == _NAME and c.after_reply[:1] == bytes([_TLS_HANDSHAKE])
    ]


def _poll_for_browser_connection(stub: SocksStub) -> bool:
    """Wait (in a worker thread, bounded) for the browser's tunnel to appear."""
    deadline = time.monotonic() + _FUSE
    while time.monotonic() < deadline:
        if _browser_connections(stub):
            return True
        time.sleep(0.05)
    return False


async def _wait_for_browser_connection(stub: SocksStub) -> None:
    assert await asyncio.to_thread(_poll_for_browser_connection, stub)


@pytest.mark.asyncio
async def test_browser_reaches_the_name_through_the_relay_and_the_proxy(
    private_tmp: str,
) -> None:
    with SocksStub() as stub:
        task = asyncio.ensure_future(
            fetch_ozon_access(sandbox=False, home_url=_HOME, proxy=_proxy(stub))
        )
        try:
            await _wait_for_browser_connection(stub)
            await observe_chromium_environments(private_tmp)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, _FUSE)
        connections = list(stub.connections)

    browser = _browser_connections(stub)
    prechecks = [c for c in connections if c.after_reply == b""]
    assert len(prechecks) == 1  # exactly one pre-check, before the browser
    assert connections[0] is prechecks[0]
    assert (prechecks[0].atyp, prechecks[0].host, prechecks[0].port) == (
        3,
        _NAME,
        443,
    )
    assert browser  # the browser's own tunnel, told apart by its ClientHello
    # Everything the browser did went through the relay: authorised, by name, 443.
    for seen in connections:
        assert (seen.username, seen.password) == (USERNAME, PASSWORD)
        assert (seen.atyp, seen.port) == (3, 443)


@pytest.mark.asyncio
async def test_without_a_proxy_the_browser_never_touches_the_stand_in(
    private_tmp: str,
) -> None:
    with SocksStub() as stub, pytest.raises(OzonAccessFetchError) as caught:
        await asyncio.wait_for(fetch_ozon_access(sandbox=False, home_url=_HOME), _FUSE)

    assert caught.value.stage is OzonAccessStage.launch_failed
    assert stub.connections == []
    assert processes_started_by_test(private_tmp) == {}


@pytest.mark.asyncio
async def test_cancellation_leaves_no_relay_socket_and_no_chromium(
    private_tmp: str,
) -> None:
    with SocksStub() as stub:
        listening_idle = _listening_loopback_ports()
        task = asyncio.ensure_future(
            fetch_ozon_access(sandbox=False, home_url=_HOME, proxy=_proxy(stub))
        )
        await _wait_for_browser_connection(stub)
        environments = await observe_chromium_environments(private_tmp)
        listening_busy = _listening_loopback_ports()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _FUSE)
        listening_after = _listening_loopback_ports()
        after = processes_started_by_test(private_tmp)

    assert environments  # positive control: a browser really ran ...
    assert len(listening_busy - listening_idle) == 1  # ... with the relay's port
    assert listening_after == listening_idle
    assert after == {}


@pytest.mark.asyncio
async def test_deadline_leaves_no_relay_socket_and_no_chromium(
    private_tmp: str,
) -> None:
    with SocksStub() as stub:
        listening_idle = _listening_loopback_ports()
        provider = BrowserOzonAccessProvider(
            lambda: fetch_ozon_access(
                sandbox=False, home_url=_HOME, proxy=_proxy(stub)
            ),
            token_timeout_seconds=8.0,
            failure_pause_seconds=300.0,
            launch_budget=8,
            launch_window_seconds=900.0,
        )
        try:
            result = await asyncio.wait_for(provider.get(), _FUSE)
        finally:
            await provider.aclose()
        listening_after = _listening_loopback_ports()
        after = processes_started_by_test(private_tmp)

    assert result == OzonAccessUnavailable(
        OzonAccessStage.challenge_timeout, "no access within 8 s"
    )
    assert _browser_connections(stub)  # the browser did go through the relay
    assert listening_after == listening_idle
    assert after == {}
