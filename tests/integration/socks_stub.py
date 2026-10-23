"""A local SOCKS5 server with user/password authorisation (not a real proxy).

Served on `127.0.0.1` in threads, in the spirit of `ozon_stub.py`, so the real
`SocksRelay`, `check_proxy`, `curl_cffi` and Chromium can talk to it
(`PROJECT.md` 2.14 O10). It writes down, per connection, what the client said
(RFC 1928 methods, RFC 1929 credentials, ATYP, host, port, the first bytes
after the reply), so a test asserts on the wire and not on the code.

Modes (`SocksStub(mode=...)`):

- `tunnel`: authorise, answer `CONNECT`, then relay bytes to the address in
  `targets[host]` (or `default_target`); with no target the connection is
  accepted and read until the client closes;
- `reject_method` (`0xFF`), `reject_auth` (status 1), `reject_connect`
  (reply 5), `close` (accept and close at once), `hang` (read, never answer);
- malformed answers: `bad_version`, `bad_method`, `bad_auth_version`,
  `bad_connect_version`, `bad_atyp`;
- `reset_after_reply`: answer `CONNECT`, then reset the connection.

`drop_first=N` resets the first N connections right after the greeting,
whatever the mode (a proxy dropping tunnels at random).
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
from dataclasses import dataclass
from dataclasses import field
from types import TracebackType

USERNAME = "stub-login"
PASSWORD = "stub-pass"
_CHUNK = 65536


@dataclass
class SocksConnection:
    """What one client connection said to the stub."""

    methods: bytes = b""
    username: str | None = None
    password: str | None = None
    atyp: int | None = None
    host: str | None = None
    port: int | None = None
    after_reply: bytearray = field(default_factory=bytearray)
    closed: threading.Event = field(default_factory=threading.Event)


def _stop_listener(listener: socket.socket) -> None:
    """Wake a thread blocked in `accept` (a bare `close` does not on Linux)."""
    with contextlib.suppress(OSError):
        listener.shutdown(socket.SHUT_RDWR)
    listener.close()


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("client closed")
        data += chunk
    return data


def _pump(source: socket.socket, target: socket.socket, sink: bytearray | None) -> None:
    """Copy until EOF or an error, then half-close the target."""
    try:
        while data := source.recv(_CHUNK):
            if sink is not None and len(sink) < 64:
                sink.extend(data[: 64 - len(sink)])
            target.sendall(data)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        target.shutdown(socket.SHUT_WR)


class SocksStub:
    """Start with `with SocksStub() as stub:`; `stub.port` is the proxy port."""

    def __init__(
        self,
        *,
        mode: str = "tunnel",
        require_auth: bool = True,
        bound_atyp: int = 1,
        targets: dict[str, int] | None = None,
        default_target: int | None = None,
        username: str = USERNAME,
        password: str = PASSWORD,
        drop_first: int = 0,
    ) -> None:
        self.mode = mode
        self.drop_first = drop_first
        self.username = username
        self.password = password
        self.require_auth = require_auth
        self.bound_atyp = bound_atyp
        self.targets = targets or {}
        self.default_target = default_target
        self.connections: list[SocksConnection] = []
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._release = threading.Event()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port = int(self._listener.getsockname()[1])
        self._stopping = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def __enter__(self) -> SocksStub:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stopping = True
        self._release.set()
        _stop_listener(self._listener)
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        self._thread.join(timeout=5)

    def wait_all_closed(self, timeout: float = 10.0) -> bool:
        """True once every connection seen so far has been closed by its client."""
        with self._lock:
            records = list(self.connections)
        return all(rec.closed.wait(timeout) for rec in records)

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            record = SocksConnection()
            with self._lock:
                self.connections.append(record)
                self._sockets.append(conn)
            threading.Thread(
                target=self._serve, args=(conn, record), daemon=True
            ).start()

    def _serve(self, conn: socket.socket, rec: SocksConnection) -> None:
        try:
            self._dialogue(conn, rec)
        except ConnectionError, OSError:
            pass
        finally:
            conn.close()
            rec.closed.set()

    def _dialogue(self, conn: socket.socket, rec: SocksConnection) -> None:
        with self._lock:
            index = self.connections.index(rec)
        if index < self.drop_first:
            conn.recv(_CHUNK)
            conn.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            return
        mode = self.mode
        if mode == "close":
            # Read the greeting first: closing with unread data would be a reset.
            conn.recv(_CHUNK)
            return
        if mode == "hang":
            while conn.recv(_CHUNK):
                pass
            return
        _version, count = _recv_exact(conn, 2)
        rec.methods = _recv_exact(conn, count)
        if mode == "reject_method":
            conn.sendall(b"\x05\xff")
            return
        if mode == "bad_version":
            conn.sendall(b"\x04\x00")
            return
        if mode == "bad_method":
            conn.sendall(b"\x05\x01")
            return
        if self.require_auth:
            conn.sendall(b"\x05\x02")
            _auth_version, user_len = _recv_exact(conn, 2)
            user = _recv_exact(conn, user_len)
            (pass_len,) = _recv_exact(conn, 1)
            password = _recv_exact(conn, pass_len)
            rec.username = user.decode()
            rec.password = password.decode()
            if mode == "bad_auth_version":
                conn.sendall(b"\x02\x00")
                return
            accepted = (
                mode != "reject_auth"
                and rec.username == self.username
                and rec.password == self.password
            )
            conn.sendall(b"\x01\x00" if accepted else b"\x01\x01")
            if not accepted:
                return
        else:
            conn.sendall(b"\x05\x00")
        _version, _cmd, _rsv, atyp = _recv_exact(conn, 4)
        rec.atyp = atyp
        if atyp == 3:
            (length,) = _recv_exact(conn, 1)
            rec.host = _recv_exact(conn, length).decode()
        elif atyp == 1:
            rec.host = socket.inet_ntop(socket.AF_INET, _recv_exact(conn, 4))
        else:
            rec.host = socket.inet_ntop(socket.AF_INET6, _recv_exact(conn, 16))
        (rec.port,) = struct.unpack(">H", _recv_exact(conn, 2))
        if mode == "reject_connect":
            conn.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
            return
        if mode == "bad_connect_version":
            conn.sendall(b"\x04\x00\x00\x01" + bytes(6))
            return
        conn.sendall(self._connect_reply())
        if mode == "bad_atyp":
            return
        if mode == "reset_after_reply":
            # Close with a zero linger: the peer sees a reset, not an EOF.
            conn.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            return
        self._tunnel(conn, rec)

    def _connect_reply(self) -> bytes:
        if self.mode == "bad_atyp":
            return b"\x05\x00\x00\x07"
        if self.bound_atyp == 3:
            name = b"bound.example"
            return b"\x05\x00\x00\x03" + bytes([len(name)]) + name + b"\x00\x50"
        if self.bound_atyp == 4:
            return b"\x05\x00\x00\x04" + bytes(16) + b"\x00\x50"
        return b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x50"

    def _tunnel(self, conn: socket.socket, rec: SocksConnection) -> None:
        port = self.targets.get(rec.host or "", self.default_target)
        if port is None:
            _swallow(conn, rec.after_reply)
            return
        target = socket.create_connection(("127.0.0.1", port))
        with self._lock:
            self._sockets.append(target)
        back = threading.Thread(target=_pump, args=(target, conn, None), daemon=True)
        back.start()
        _pump(conn, target, rec.after_reply)
        back.join(timeout=10)
        target.close()


def _swallow(source: socket.socket, sink: bytearray) -> None:
    """Read until EOF or an error, keeping the first bytes (a tunnel to nowhere)."""
    try:
        while data := source.recv(_CHUNK):
            if len(sink) < 64:
                sink.extend(data[: 64 - len(sink)])
    except OSError:
        pass


class EchoServer:
    """A TCP server that sends back every byte it receives."""

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> EchoServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _stop_listener(self._listener)
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        with conn:
            try:
                while data := conn.recv(_CHUNK):
                    conn.sendall(data)
            except OSError:
                pass


class SinkServer:
    """A TCP server that reads and discards everything; counts the bytes."""

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = int(self._listener.getsockname()[1])
        self.received = 0
        self.drained = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> SinkServer:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _stop_listener(self._listener)
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._drain, args=(conn,), daemon=True).start()

    def _drain(self, conn: socket.socket) -> None:
        with conn:
            try:
                while data := conn.recv(_CHUNK):
                    self.received += len(data)
            except OSError:
                pass
            finally:
                self.drained.set()
