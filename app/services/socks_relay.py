"""Outbound SOCKS5 proxy for Ozon (`PROJECT.md` section 2.14, O10, O11).

Chromium from `playwright` cannot use a SOCKS5 proxy with a login, while the
customer's proxy is exactly that. So the browser goes through a local relay of
one acquisition: HTTP `CONNECT` on `127.0.0.1:<random port>` chained to the
SOCKS5 upstream (RFC 1928, user/password RFC 1929). `curl_cffi` speaks SOCKS5
itself and takes `SocksProxy.curl_url`.

Relay rules (R3): only `CONNECT`, only to port 443; the request head is capped
at 8 KiB and read under a deadline; the host goes to the upstream as a domain
name (ATYP 3, an IP literal as an address), the relay does no DNS itself, so
the exit and the name resolution stay on the proxy side. The host is not
restricted (the Ozon page loads resources from other domains). Named risk:
while the relay lives, any process of the container can use it. The relay
exists for one acquisition and is closed on every exit, cancellation included.

`check_proxy` is the pre-check of R2: handshake, authorisation and `CONNECT`
through the proxy, then close. Failures carry a closed set of `DETAIL_PROXY_*`
constants; they never contain the proxy host or the credentials.

A tunnel the proxy drops while it is being opened (a reset or a close in the
handshake, a refused connection) is opened again, up to `_TUNNEL_ATTEMPTS`
times: the customer's proxy drops a share of tunnels at random (measured from
the server on 2026-10-23: 2 of 20), and one page of Ozon opens dozens of them.
A missed deadline and an explicit refusal (authorisation, `CONNECT`) are not
retried. The relay remembers the last upstream refusal (`upstream_failure`),
so the browser step can tell a proxy failure from an Ozon one.

Secrets: host, login and password are `MaskedStr`, `repr` of `SocksProxy` and
of the relay hides everything, and the authorisation request is built by a
helper returning a masked value, so a traceback with locals does not show it.
The URL rules are checked once, by the settings validator (R1);
`SocksProxy.from_url` only splits an already verified URL.
"""

import asyncio
import contextlib
import ipaddress
import struct
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlsplit

from loguru import logger

from app.services.ozon_access import MaskedStr

DETAIL_PROXY_AUTH_FAILED = "proxy_auth_failed"
DETAIL_PROXY_UNREACHABLE = "proxy_unreachable"
DETAIL_PROXY_CONNECT_REJECTED = "proxy_connect_rejected"
DETAIL_PROXY_PROTOCOL_ERROR = "proxy_protocol_error"

_ALLOWED_PORT = 443
_MAX_HEAD_BYTES = 8192
_HEAD_TIMEOUT_SECONDS = 10.0
_HANDSHAKE_TIMEOUT_SECONDS = 10.0
_TUNNEL_ATTEMPTS = 3
_TUNNEL_RETRY_DELAY_SECONDS = 0.2
_CHUNK_BYTES = 65536
_DRAIN_ACCEPT_SECONDS = 0.05
_LOOPBACK = "127.0.0.1"

_SOCKS_VERSION = 5
_METHOD_NONE = 0
_METHOD_PASSWORD = 2
_METHOD_REFUSED = 0xFF
_AUTH_VERSION = 1
_CMD_CONNECT = 1
_ATYP_IPV4 = 1
_ATYP_DOMAIN = 3
_ATYP_IPV6 = 4

_MASK = "***"


class _MaskedBytes(bytes):
    """`bytes` whose `repr` hides the value (traceback locals, `pytest -l`)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return repr(_MASK)


class SocksError(Exception):
    """A proxy refusal; `detail` is one of the `DETAIL_PROXY_*` constants.

    `dropped` marks a tunnel the proxy dropped while it was being opened: the
    cue for `_open_tunnel` to try again.
    """

    def __init__(self, detail: str, *, dropped: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.dropped = dropped


@dataclass(frozen=True, slots=True, repr=False)
class SocksProxy:
    """A SOCKS5 proxy with optional login and password (both or neither)."""

    host: MaskedStr
    port: int
    username: MaskedStr | None = None
    password: MaskedStr | None = None

    def __repr__(self) -> str:
        return "SocksProxy(<masked>)"

    @classmethod
    def from_url(cls, url: str) -> Self:
        """Split a `socks5://` or `socks5h://` URL; credentials are percent-decoded.

        Precondition: the URL passed the settings validator (scheme, host,
        port, login/password pair and their sizes, no path/query/fragment).
        The rules are not repeated here (R1).
        """
        parts = urlsplit(url)
        # `hostname` of a bracketed IPv6 literal comes without brackets.
        host = parts.hostname or ""
        port = parts.port or 0
        if parts.username is None or parts.password is None:
            return cls(host=MaskedStr(host), port=port)
        return cls(
            host=MaskedStr(host),
            port=port,
            username=MaskedStr(unquote(parts.username)),
            password=MaskedStr(unquote(parts.password)),
        )

    @property
    def curl_url(self) -> MaskedStr:
        """URL for `curl_cffi`: always `socks5h` (DNS on the proxy side)."""
        host = f"[{self.host}]" if ":" in self.host else str(self.host)
        auth = ""
        if self.username is not None and self.password is not None:
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return MaskedStr(f"socks5h://{auth}{host}:{self.port}")


def _auth_request(proxy: SocksProxy) -> _MaskedBytes:
    """RFC 1929 request. A separate function: its locals are gone on a failure."""
    user = (proxy.username or "").encode()
    password = (proxy.password or "").encode()
    return _MaskedBytes(
        bytes([_AUTH_VERSION, len(user)]) + user + bytes([len(password)]) + password
    )


def _address(host: str) -> bytes:
    """ATYP and address of a CONNECT target: IP literal as is, else a domain."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("ascii")
        return bytes([_ATYP_DOMAIN, len(name)]) + name
    return bytes([_ATYP_IPV4 if ip.version == 4 else _ATYP_IPV6]) + ip.packed


async def _handshake(
    proxy: SocksProxy,
    host: str,
    port: int,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Methods, RFC 1929 authorisation if configured, then `CONNECT` to host:port."""
    authorised = proxy.username is not None
    method = _METHOD_PASSWORD if authorised else _METHOD_NONE
    writer.write(bytes([_SOCKS_VERSION, 1, method]))
    version, chosen = await reader.readexactly(2)
    if version != _SOCKS_VERSION:
        raise SocksError(DETAIL_PROXY_PROTOCOL_ERROR)
    if chosen == _METHOD_REFUSED:
        raise SocksError(DETAIL_PROXY_AUTH_FAILED)
    if chosen != method:
        raise SocksError(DETAIL_PROXY_PROTOCOL_ERROR)
    if authorised:
        writer.write(_auth_request(proxy))
        version, status = await reader.readexactly(2)
        if version != _AUTH_VERSION:
            raise SocksError(DETAIL_PROXY_PROTOCOL_ERROR)
        if status != 0:
            raise SocksError(DETAIL_PROXY_AUTH_FAILED)
    writer.write(
        bytes([_SOCKS_VERSION, _CMD_CONNECT, 0])
        + _address(host)
        + struct.pack(">H", port)
    )
    version, reply, _reserved, atyp = await reader.readexactly(4)
    if version != _SOCKS_VERSION:
        raise SocksError(DETAIL_PROXY_PROTOCOL_ERROR)
    if reply != 0:
        raise SocksError(DETAIL_PROXY_CONNECT_REJECTED)
    if atyp == _ATYP_IPV4:
        bound = 4
    elif atyp == _ATYP_IPV6:
        bound = 16
    elif atyp == _ATYP_DOMAIN:
        bound = (await reader.readexactly(1))[0]
    else:
        raise SocksError(DETAIL_PROXY_PROTOCOL_ERROR)
    await reader.readexactly(bound + 2)


async def _open_tunnel(
    proxy: SocksProxy, host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """`_open_tunnel_once`, repeated while the proxy drops the tunnel.

    Raises the `SocksError` of the last attempt.
    """
    attempt = 1
    while True:
        try:
            return await _open_tunnel_once(proxy, host, port)
        except SocksError as e:
            if not e.dropped or attempt >= _TUNNEL_ATTEMPTS:
                raise
        attempt += 1
        await asyncio.sleep(_TUNNEL_RETRY_DELAY_SECONDS)


async def _open_tunnel_once(
    proxy: SocksProxy, host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the proxy and `CONNECT` to host:port; raises `SocksError`.

    The connection is closed on every failure and on cancellation.
    """
    try:
        async with asyncio.timeout(_HANDSHAKE_TIMEOUT_SECONDS):
            reader, writer = await asyncio.open_connection(
                proxy.host, proxy.port, limit=_CHUNK_BYTES
            )
            try:
                await _handshake(proxy, host, port, reader, writer)
            except BaseException:
                writer.close()
                raise
            return reader, writer
    except SocksError:
        raise
    except TimeoutError as e:
        raise SocksError(DETAIL_PROXY_UNREACHABLE) from e
    except OSError as e:
        raise SocksError(DETAIL_PROXY_UNREACHABLE, dropped=True) from e
    except asyncio.IncompleteReadError as e:
        raise SocksError(DETAIL_PROXY_PROTOCOL_ERROR, dropped=True) from e
    except (UnicodeError, ValueError) as e:
        raise SocksError(DETAIL_PROXY_PROTOCOL_ERROR) from e


async def check_proxy(proxy: SocksProxy, host: str, port: int = _ALLOWED_PORT) -> None:
    """Pre-check (R2): handshake, authorisation and `CONNECT`, then close.

    Raises `SocksError` with a `DETAIL_PROXY_*` constant on a refusal.
    """
    _reader, writer = await _open_tunnel(proxy, host, port)
    await _close(writer)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def _pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
    """Copy until EOF, then close the target (which ends the opposite pipe)."""
    try:
        while data := await source.read(_CHUNK_BYTES):
            target.write(data)
            await target.drain()
    except OSError as e:
        logger.debug(f"Ozon proxy relay: a pipe ended: {type(e).__name__}")
    finally:
        target.close()


class SocksRelay:
    """Local HTTP `CONNECT` proxy over a SOCKS5 upstream; one acquisition long.

    `async with SocksRelay(proxy) as relay:` then `relay.url` is the proxy
    address for the browser. On exit (also after a cancellation) the port stops
    accepting, every open connection is closed and no task of the relay lives.
    """

    def __init__(self, proxy: SocksProxy) -> None:
        self._proxy = proxy
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._url: str | None = None
        self._closing = False
        self.upstream_failure: str | None = None

    def __repr__(self) -> str:
        return "SocksRelay(<masked>)"

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("relay is not started")
        return self._url

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(
            self._handle, _LOOPBACK, 0, limit=_MAX_HEAD_BYTES
        )
        port = self._server.sockets[0].getsockname()[1]
        self._url = f"http://{_LOOPBACK}:{port}"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        server = self._server
        if server is None:
            return
        self._closing = True
        # The pause lets connections already in the backlog or in asyncio's
        # accept path become transports before `server.close()`: one wrapped
        # after it fails in `Server._attach`, asyncio swallows that and the
        # socket stays open. It works not because of the 50 ms but because of
        # the handler order in `_run_once` of CPython's selector loop: timers
        # run after the I/O events of the same iteration, so
        # `_accept_connection2` gets to `_attach` first. Any delay above zero
        # does; `sleep(0)` does not. Not valid for uvloop (not used).
        # The closing is in `finally`: a cancellation or deadline of the owner
        # during the pause must not leave the port listening. After the
        # delivered cancellation the awaits below are not interrupted again.
        try:
            await asyncio.sleep(_DRAIN_ACCEPT_SECONDS)
        finally:
            server.close()
            server.close_clients()
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._closing:
            # Accepted just before the exit: its task never got registered.
            writer.close()
            return
        task = asyncio.current_task()
        assert task is not None  # a stream callback always runs inside a task
        self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, TimeoutError) as e:
            logger.debug(f"Ozon proxy relay: connection ended: {type(e).__name__}")
        finally:
            writer.close()
            self._tasks.discard(task)

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, line: str) -> None:
        writer.write(f"HTTP/1.1 {line}\r\n\r\n".encode("ascii"))
        await writer.drain()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(_HEAD_TIMEOUT_SECONDS):
                head = await reader.readuntil(b"\r\n\r\n")
            if len(head) > _MAX_HEAD_BYTES:
                raise asyncio.LimitOverrunError("request head over the limit", 0)
        except asyncio.LimitOverrunError:
            logger.debug("Ozon proxy relay: request head over the limit")
            return
        request = head.split(b"\r\n", 1)[0].split(b" ")
        if len(request) != 3:
            await self._reply(writer, "400 Bad Request")
            return
        if request[0] != b"CONNECT":
            await self._reply(writer, "405 Method Not Allowed")
            return
        target = self._parse_target(request[1])
        if target is None:
            await self._reply(writer, "400 Bad Request")
            return
        host, port = target
        if port != _ALLOWED_PORT:
            await self._reply(writer, "403 Forbidden")
            return
        try:
            up_reader, up_writer = await _open_tunnel(self._proxy, host, port)
        except SocksError as e:
            self.upstream_failure = e.detail
            logger.warning(f"Ozon proxy relay: upstream refused: {e.detail}")
            await self._reply(writer, "502 Bad Gateway")
            return
        try:
            await self._reply(writer, "200 Connection Established")
            async with asyncio.TaskGroup() as group:
                group.create_task(_pipe(reader, up_writer))
                group.create_task(_pipe(up_reader, writer))
        finally:
            up_writer.close()

    @staticmethod
    def _parse_target(raw: bytes) -> tuple[str, int] | None:
        """`host:port` of a CONNECT line, or None if it is malformed."""
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        host, _, port_text = text.rpartition(":")
        host = host.removeprefix("[").removesuffix("]")
        if not 0 < len(host) <= 255 or not (
            port_text.isascii() and port_text.isdigit()
        ):
            return None
        return host, int(port_text)
