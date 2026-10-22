"""`SocksRelay` and `check_proxy` on real sockets against a SOCKS5 stand-in (PAB-111).

The upstream is `tests/integration/socks_stub.py` (a real server on `127.0.0.1`,
writes down what the client said on the wire). Nothing here talks to a network
outside the loopback. Expected values are literals of RFC 1928 / RFC 1929 and of
the ticket, not read from the module.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
import tracemalloc
from urllib.parse import urlsplit

import pytest

from app.services import socks_relay
from app.services.socks_relay import SocksProxy
from app.services.socks_relay import SocksRelay
from app.services.socks_relay import check_proxy
from tests.integration.socks_stub import PASSWORD
from tests.integration.socks_stub import USERNAME
from tests.integration.socks_stub import EchoServer
from tests.integration.socks_stub import SinkServer
from tests.integration.socks_stub import SocksStub

_FUSE = 15.0
_PROMPT = 3.0  # far below the 10 s head deadline: closed by the exit, not by it
_OK = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def _proxy(stub: SocksStub) -> SocksProxy:
    return SocksProxy.from_url(f"socks5://{USERNAME}:{PASSWORD}@127.0.0.1:{stub.port}")


def _endpoint(relay: SocksRelay) -> tuple[str, int]:
    parts = urlsplit(relay.url)
    assert parts.hostname is not None
    assert parts.port is not None
    return parts.hostname, parts.port


async def _open(
    relay: SocksRelay,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    host, port = _endpoint(relay)
    return await asyncio.wait_for(asyncio.open_connection(host, port), _FUSE)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def _exchange(relay: SocksRelay, payload: bytes) -> bytes:
    """Send `payload`, return everything the relay answers until it closes.

    A reset (the relay closed with our bytes unread) counts as the end.
    """
    reader, writer = await _open(relay)
    try:
        with contextlib.suppress(ConnectionError):
            writer.write(payload)
            await writer.drain()
        try:
            return await asyncio.wait_for(reader.read(), _FUSE)
        except ConnectionResetError:
            return b""
    finally:
        await _close(writer)


async def _tunnel(
    relay: SocksRelay, target: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a tunnel through the relay; the `200` line is consumed."""
    reader, writer = await _open(relay)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _FUSE)
    assert head == _OK
    return reader, writer


# --- the three request rules (a), (b), (c) -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS", "connect"])
async def test_non_connect_gets_405_and_no_upstream_connection(method: str) -> None:
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            answer = await _exchange(
                relay, f"{method} http://ozon.test/ HTTP/1.1\r\n\r\n".encode()
            )

    assert answer == b"HTTP/1.1 405 Method Not Allowed\r\n\r\n"
    assert stub.connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [80, 442, 444, 8443, 0, 65535])
async def test_connect_to_a_port_other_than_443_gets_403_and_no_upstream(
    port: int,
) -> None:
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            answer = await _exchange(
                relay, f"CONNECT ozon.test:{port} HTTP/1.1\r\n\r\n".encode()
            )

    assert answer == b"HTTP/1.1 403 Forbidden\r\n\r\n"
    assert stub.connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_line",
    [
        b"CONNECT ozon.test:443",
        b"CONNECT ozon.test:443 HTTP/1.1 extra",
        b"CONNECT ozon.test HTTP/1.1",
        b"CONNECT :443 HTTP/1.1",
        b"CONNECT ozon.test:44x HTTP/1.1",
        b"CONNECT ozon.test: HTTP/1.1",
        b"CONNECT [] HTTP/1.1",
        b"CONNECT \xd0\xbf.test:443 HTTP/1.1",
        b"CONNECT " + b"a" * 256 + b":443 HTTP/1.1",
    ],
    ids=[
        "two-parts",
        "four-parts",
        "no-port",
        "empty-host",
        "non-digit-port",
        "empty-port",
        "empty-brackets",
        "non-ascii-host",
        "host-over-255",
    ],
)
async def test_malformed_connect_line_gets_400_and_no_upstream(
    request_line: bytes,
) -> None:
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            answer = await _exchange(relay, request_line + b"\r\n\r\n")

    assert answer == b"HTTP/1.1 400 Bad Request\r\n\r\n"
    assert stub.connections == []


@pytest.mark.asyncio
async def test_request_head_over_the_limit_is_closed_without_an_answer() -> None:
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            answer = await _exchange(relay, b"CONNECT " + b"a" * 9000)

    assert answer == b""
    assert stub.connections == []


def _head_of(size: int) -> bytes:
    prefix = b"CONNECT ozon.test:443 HTTP/1.1\r\nX: "
    head = prefix + b"a" * (size - len(prefix) - 4) + b"\r\n\r\n"
    assert len(head) == size
    return head


@pytest.mark.asyncio
async def test_request_head_of_exactly_8_kib_is_still_served() -> None:
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            reader, writer = await _open(relay)
            try:
                writer.write(_head_of(8192))
                await writer.drain()
                answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _FUSE)
            finally:
                await _close(writer)

    assert answer == _OK
    assert [c.host for c in stub.connections] == ["ozon.test"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [8193, 8194, 8196, 8197])
async def test_request_head_one_byte_over_8_kib_is_closed_without_an_answer(
    size: int,
) -> None:
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            answer = await _exchange(relay, _head_of(size))

    assert answer == b""
    assert stub.connections == []


@pytest.mark.asyncio
async def test_silent_client_is_closed_after_the_head_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socks_relay, "_HEAD_TIMEOUT_SECONDS", 0.3)
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            reader, writer = await _open(relay)
            try:
                data = await asyncio.wait_for(reader.read(), _FUSE)
            finally:
                await _close(writer)

    assert data == b""
    assert stub.connections == []


@pytest.mark.asyncio
async def test_client_leaving_in_the_middle_of_the_head_gets_no_upstream() -> None:
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            reader, writer = await _open(relay)
            writer.write(b"CONNECT ozon.test:443 HT")
            await writer.drain()
            writer.write_eof()
            data = await asyncio.wait_for(reader.read(), _FUSE)
            await _close(writer)

    assert data == b""
    assert stub.connections == []


# --- (g) the tunnel ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_reaches_the_upstream_with_credentials_domain_and_port() -> None:
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            _reader, writer = await _tunnel(relay, "ozon.test:443")
            await _close(writer)
        assert stub.wait_all_closed()

    [seen] = stub.connections
    assert seen.methods == b"\x02"
    assert (seen.username, seen.password) == (USERNAME, PASSWORD)
    assert (seen.atyp, seen.host, seen.port) == (3, "ozon.test", 443)


@pytest.mark.asyncio
async def test_data_goes_both_ways_without_distortion() -> None:
    payload = bytes(range(256)) * 700  # 179200 bytes: several relay chunks
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            reader, writer = await _tunnel(relay, "ozon.test:443")
            writer.write(payload)
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(payload)), _FUSE)
            await _close(writer)

    assert echoed == payload
    assert bytes(stub.connections[0].after_reply[:16]) == payload[:16]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "atyp", "host"),
    [("127.0.0.1:443", 1, "127.0.0.1"), ("[::1]:443", 4, "::1")],
    ids=["ipv4", "ipv6"],
)
async def test_ip_literal_target_goes_to_the_upstream_as_an_address(
    target: str, atyp: int, host: str
) -> None:
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            _reader, writer = await _tunnel(relay, target)
            await _close(writer)

    [seen] = stub.connections
    assert (seen.atyp, seen.host, seen.port) == (atyp, host, 443)


@pytest.mark.asyncio
async def test_proxy_without_credentials_is_asked_for_no_authentication() -> None:
    with SocksStub(require_auth=False) as stub:
        proxy = SocksProxy.from_url(f"socks5://127.0.0.1:{stub.port}")
        async with SocksRelay(proxy) as relay:
            _reader, writer = await _tunnel(relay, "ozon.test:443")
            await _close(writer)

    [seen] = stub.connections
    assert seen.methods == b"\x00"
    assert seen.username is None
    assert (seen.atyp, seen.host, seen.port) == (3, "ozon.test", 443)


@pytest.mark.asyncio
async def test_each_connect_gets_its_own_upstream_connection() -> None:
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            first = await _tunnel(relay, "a.test:443")
            second = await _tunnel(relay, "b.test:443")
            await _close(first[1])
            await _close(second[1])

    assert sorted(c.host or "" for c in stub.connections) == ["a.test", "b.test"]


# --- (d) upstream failures ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "reject_auth",
        "reject_method",
        "reject_connect",
        "bad_version",
        "bad_method",
        "bad_auth_version",
        "bad_connect_version",
        "bad_atyp",
        "close",
    ],
)
async def test_failing_upstream_gives_502_and_the_connection_is_closed(
    mode: str,
) -> None:
    with SocksStub(mode=mode) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            answer = await _exchange(relay, b"CONNECT ozon.test:443 HTTP/1.1\r\n\r\n")

    assert answer == b"HTTP/1.1 502 Bad Gateway\r\n\r\n"


@pytest.mark.asyncio
async def test_unreachable_upstream_gives_502() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    proxy = SocksProxy.from_url(f"socks5://u:p@127.0.0.1:{port}")
    async with SocksRelay(proxy) as relay:
        answer = await _exchange(relay, b"CONNECT ozon.test:443 HTTP/1.1\r\n\r\n")

    assert answer == b"HTTP/1.1 502 Bad Gateway\r\n\r\n"


@pytest.mark.asyncio
async def test_hanging_upstream_gives_502_after_the_handshake_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socks_relay, "_HANDSHAKE_TIMEOUT_SECONDS", 0.3)
    with SocksStub(mode="hang") as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            answer = await _exchange(relay, b"CONNECT ozon.test:443 HTTP/1.1\r\n\r\n")

    assert answer == b"HTTP/1.1 502 Bad Gateway\r\n\r\n"


@pytest.mark.asyncio
async def test_upstream_reset_in_the_middle_closes_the_client_side_too() -> None:
    with SocksStub(mode="reset_after_reply") as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            reader, writer = await _tunnel(relay, "ozon.test:443")  # established
            try:
                data = await asyncio.wait_for(reader.read(), _FUSE)
            except ConnectionResetError:
                data = b""
            finally:
                await _close(writer)

    assert data == b""
    assert [c.host for c in stub.connections] == ["ozon.test"]


# --- (e) lifecycle ----------------------------------------------------------------


async def _assert_relay_is_gone(
    stub: SocksStub,
    endpoint: tuple[str, int],
    reader: asyncio.StreamReader,
    tasks_before: set[asyncio.Task[object]],
) -> None:
    with pytest.raises(OSError):
        await asyncio.wait_for(asyncio.open_connection(*endpoint), _FUSE)
    assert await asyncio.wait_for(reader.read(), _FUSE) == b""
    assert await asyncio.to_thread(stub.wait_all_closed)
    assert asyncio.all_tasks() - tasks_before == set()


@pytest.mark.asyncio
async def test_exit_closes_the_port_every_connection_and_leaves_no_task() -> None:
    tasks_before = asyncio.all_tasks()
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            endpoint = _endpoint(relay)
            reader, writer = await _tunnel(relay, "ozon.test:443")
            writer.write(b"ping")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), _FUSE) == b"ping"
        try:
            await _assert_relay_is_gone(stub, endpoint, reader, tasks_before)
        finally:
            await _close(writer)


@pytest.mark.asyncio
async def test_cancelling_the_owner_closes_the_port_every_connection_and_tasks() -> (
    None
):
    tasks_before = asyncio.all_tasks()
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        ready = asyncio.Event()
        endpoints: list[tuple[str, int]] = []
        relays: list[SocksRelay] = []

        async def owner() -> None:
            async with SocksRelay(_proxy(stub)) as relay:
                relays.append(relay)
                endpoints.append(_endpoint(relay))
                ready.set()
                await asyncio.Event().wait()

        task = asyncio.ensure_future(owner())
        await asyncio.wait_for(ready.wait(), _FUSE)
        reader, writer = await _tunnel(relays[0], "ozon.test:443")
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(4), _FUSE) == b"ping"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _FUSE)
        try:
            await _assert_relay_is_gone(stub, endpoints[0], reader, tasks_before)
        finally:
            await _close(writer)


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", range(10))
async def test_connection_accepted_just_before_exit_is_closed_with_no_upstream(
    turns: int,
) -> None:
    """A client connects and the owner leaves after `turns` event-loop turns:
    whichever stage the accepted connection has reached, it ends closed, with
    no upstream connection and no task left."""
    tasks_before = asyncio.all_tasks()
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            host, port = _endpoint(relay)
            client = socket.create_connection((host, port))
            for _ in range(turns):
                await asyncio.sleep(0)
        try:
            client.settimeout(_PROMPT)
            data = b""
            with contextlib.suppress(ConnectionResetError):
                data = await asyncio.to_thread(client.recv, 1024)
        finally:
            client.close()

    assert data == b""
    assert stub.connections == []
    assert asyncio.all_tasks() - tasks_before == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["deadline", "cancel"])
async def test_owner_stopped_during_the_exit_pause_still_closes_port_and_tasks(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The deadline or the cancellation of the owner lands while `__aexit__`
    waits (the pause is stretched to 0.5 s to make the window certain): the
    closing steps still run."""
    monkeypatch.setattr(socks_relay, "_DRAIN_ACCEPT_SECONDS", 0.5)
    tasks_before = asyncio.all_tasks()
    with EchoServer() as echo, SocksStub(default_target=echo.port) as stub:
        endpoints: list[tuple[str, int]] = []
        body_done = asyncio.Event()

        async def owner() -> None:
            async with SocksRelay(_proxy(stub)) as relay:
                endpoints.append(_endpoint(relay))
                reader, writer = await _tunnel(relay, "ozon.test:443")
                writer.write(b"ping")
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(4), _FUSE) == b"ping"
                body_done.set()

        if how == "deadline":
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await owner()
        else:
            task = asyncio.ensure_future(owner())
            await asyncio.wait_for(body_done.wait(), _FUSE)
            await asyncio.sleep(0.1)  # inside the pause
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, _FUSE)

        with pytest.raises(OSError):
            await asyncio.wait_for(asyncio.open_connection(*endpoints[0]), _FUSE)
        assert await asyncio.to_thread(stub.wait_all_closed)

    assert asyncio.all_tasks() - tasks_before == set()


def _connect_without_waiting(host: str, port: int, sink: list[socket.socket]) -> None:
    """Start a TCP connection from a timer callback; the loop is not blocked."""
    client = socket.socket()
    client.setblocking(False)
    client.connect_ex((host, port))
    sink.append(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("lead_ms", [10, 6, 4, 3, 2, 1, 0])
async def test_connection_landing_at_the_end_of_the_pause_is_closed_with_no_upstream(
    monkeypatch: pytest.MonkeyPatch, lead_ms: int
) -> None:
    """A client connects `lead_ms` before the pause of `__aexit__` ends, so its
    transport and first handler step come after the owner has taken the tasks
    to cancel. Whatever the stage, the connection ends closed (or refused), the
    upstream never sees it and no task is left."""
    pause = 0.3
    monkeypatch.setattr(socks_relay, "_DRAIN_ACCEPT_SECONDS", pause)
    tasks_before = asyncio.all_tasks()
    clients: list[socket.socket] = []
    with SocksStub() as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            host, port = _endpoint(relay)
            asyncio.get_running_loop().call_later(
                pause - lead_ms / 1000,
                _connect_without_waiting,
                host,
                port,
                clients,
            )
        assert len(clients) == 1  # the callback ran before the exit returned
        [client] = clients
        client.setblocking(True)
        client.settimeout(_PROMPT)
        try:
            data = b"?"
            with contextlib.suppress(ConnectionError):  # reset/refused: closed too
                data = await asyncio.to_thread(client.recv, 1024)
        finally:
            client.close()

    assert data in (b"", b"?")
    assert stub.connections == []
    assert asyncio.all_tasks() - tasks_before == set()


def _connect_then_stall_the_loop(
    host: str, port: int, sink: list[socket.socket], stall_seconds: float
) -> None:
    """Connect without waiting, then hold the loop past the pause deadline.

    On the next loop turn the listener is readable and the pause timer is due
    in the same turn: the accept path and the wake-up of `__aexit__` are
    queued together, so the transport and the first step of its handler come
    after `__aexit__` has taken `self._tasks` (a deliberate, bounded stall).
    """
    _connect_without_waiting(host, port, sink)
    time.sleep(stall_seconds)


@pytest.mark.asyncio
async def test_connection_accepted_in_the_turn_the_pause_ends_is_closed_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pause = 0.1
    monkeypatch.setattr(socks_relay, "_DRAIN_ACCEPT_SECONDS", pause)
    tasks_before = asyncio.all_tasks()
    clients: list[socket.socket] = []
    with SocksStub() as stub:
        # The exit must not wait for the connection's own 10 s head deadline
        # (`wait_closed` does, if the connection was left open).
        async with asyncio.timeout(_PROMPT), SocksRelay(_proxy(stub)) as relay:
            host, port = _endpoint(relay)
            asyncio.get_running_loop().call_later(
                pause / 2, _connect_then_stall_the_loop, host, port, clients, pause
            )
        assert len(clients) == 1
        [client] = clients
        client.setblocking(True)
        client.settimeout(_PROMPT)
        try:
            data = b"?"
            with contextlib.suppress(ConnectionError):
                data = await asyncio.to_thread(client.recv, 1024)
        finally:
            client.close()

    assert data in (b"", b"?")  # closed by the relay, not left open (a timeout fails)
    assert stub.connections == []
    assert asyncio.all_tasks() - tasks_before == set()


# --- (f) bounded memory ----------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_stays_bounded_while_a_large_body_goes_through() -> None:
    total = 32 * 1024 * 1024
    chunk = b"\x5a" * 65536
    with SinkServer() as sink, SocksStub(default_target=sink.port) as stub:
        async with SocksRelay(_proxy(stub)) as relay:
            reader, writer = await _tunnel(relay, "ozon.test:443")
            tracemalloc.start()
            try:
                tracemalloc.reset_peak()
                for _ in range(total // len(chunk)):
                    writer.write(chunk)
                    await asyncio.wait_for(writer.drain(), _FUSE)
                writer.write_eof()
                await asyncio.wait_for(reader.read(), _FUSE)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                await _close(writer)

        assert await asyncio.to_thread(sink.drained.wait, _FUSE)

    assert sink.received == total
    assert peak < 8 * 1024 * 1024


# --- check_proxy (pre-check, R2) ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_atyp", [1, 3, 4], ids=["ipv4", "domain", "ipv6"])
async def test_check_proxy_succeeds_for_every_bound_address_kind_and_closes(
    bound_atyp: int,
) -> None:
    with SocksStub(bound_atyp=bound_atyp) as stub:
        await asyncio.wait_for(check_proxy(_proxy(stub), "www.ozon.test"), _FUSE)
        assert await asyncio.to_thread(stub.wait_all_closed)

    [seen] = stub.connections
    assert seen.methods == b"\x02"
    assert (seen.username, seen.password) == (USERNAME, PASSWORD)
    assert (seen.atyp, seen.host, seen.port) == (3, "www.ozon.test", 443)


@pytest.mark.asyncio
async def test_check_proxy_sends_an_ip_literal_as_an_address() -> None:
    with SocksStub() as stub:
        await asyncio.wait_for(check_proxy(_proxy(stub), "::1"), _FUSE)

    assert (stub.connections[0].atyp, stub.connections[0].host) == (4, "::1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "detail"),
    [
        ("reject_auth", "proxy_auth_failed"),
        ("reject_method", "proxy_auth_failed"),
        ("reject_connect", "proxy_connect_rejected"),
        ("bad_version", "proxy_protocol_error"),
        ("bad_method", "proxy_protocol_error"),
        ("bad_auth_version", "proxy_protocol_error"),
        ("bad_connect_version", "proxy_protocol_error"),
        ("bad_atyp", "proxy_protocol_error"),
        ("close", "proxy_protocol_error"),
    ],
)
async def test_check_proxy_marks_a_refusal_with_one_of_the_four_constants(
    mode: str, detail: str
) -> None:
    with SocksStub(mode=mode) as stub:
        with pytest.raises(socks_relay.SocksError) as excinfo:
            await asyncio.wait_for(check_proxy(_proxy(stub), "www.ozon.test"), _FUSE)
        assert await asyncio.to_thread(stub.wait_all_closed)

    assert excinfo.value.detail == detail
    assert str(excinfo.value) == detail  # no host, no credentials in the text


@pytest.mark.asyncio
async def test_check_proxy_on_a_closed_port_is_unreachable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    proxy = SocksProxy.from_url(f"socks5://u:p@127.0.0.1:{port}")

    with pytest.raises(socks_relay.SocksError) as excinfo:
        await asyncio.wait_for(check_proxy(proxy, "www.ozon.test"), _FUSE)

    assert excinfo.value.detail == "proxy_unreachable"


@pytest.mark.asyncio
async def test_check_proxy_on_a_hanging_upstream_is_unreachable_by_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socks_relay, "_HANDSHAKE_TIMEOUT_SECONDS", 0.3)
    with SocksStub(mode="hang") as stub:
        with pytest.raises(socks_relay.SocksError) as excinfo:
            await asyncio.wait_for(check_proxy(_proxy(stub), "www.ozon.test"), _FUSE)
        assert await asyncio.to_thread(stub.wait_all_closed)

    assert excinfo.value.detail == "proxy_unreachable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host", ["пример.test", "a" * 256], ids=["non-ascii", "over-255-bytes"]
)
async def test_check_proxy_with_an_unencodable_host_is_a_protocol_error(
    host: str,
) -> None:
    with SocksStub() as stub:
        with pytest.raises(socks_relay.SocksError) as excinfo:
            await asyncio.wait_for(check_proxy(_proxy(stub), host), _FUSE)
        assert await asyncio.to_thread(stub.wait_all_closed)

    assert excinfo.value.detail == "proxy_protocol_error"
