"""A local stand-in for the Ozon anti-bot protocol (not Ozon, no network).

Served on `127.0.0.1` by the stdlib `http.server` in threads, so a real
Chromium and a real `curl_cffi` client can talk to it (`PROJECT.md` 2.14 O12).
The stand-in is not Ozon: passing a real check is proved only by the live
acceptance run outside the gate.

- `GET /` (the "home page"): a script that first sets `__Secure-ETC` and
  `abt_data` (they exist before the check is passed), reports the browser's
  own view to `/report`, and only then sets `__Secure-access-token` to a token
  issued by this response (never in `solve_challenge=False` mode).
- `GET /api`: `307` (with `Location`) unless the request carries a currently
  valid `__Secure-access-token`; with it, `200` and the customer's captured
  response (`tests/fixtures/ozon/entrypoint_3593896354.json`).
- Probes of the transport: `/redir` -> `307` to `/trap` (counted), `/big` (a
  `403` or `200` with a 16 MiB body written in chunks; `bytes_sent` counts what
  left the server), `/setcookie`, `/echo` (reflects the request), `/slow`
  (holds until released), `/body?size=N` (a `200` of exactly N bytes: the
  fixture padded with spaces).
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from urllib.parse import parse_qsl

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "ozon"
    / "entrypoint_3593896354.json"
)
BIG_BODY_BYTES = 16 * 1024 * 1024
_CHUNK = 64 * 1024

_PAGE = """<!doctype html><html><body><script>
document.cookie = "__Secure-ETC=etc-early; Secure; path=/";
document.cookie = "abt_data=abt-early; Secure; path=/";
const q = new URLSearchParams({{
  webdriver: String(navigator.webdriver),
  language: navigator.language,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  x: String(window.screenX),
  y: String(window.screenY),
}});
fetch("/report?" + q.toString()).finally(() => {{
  {set_token}
}});
</script></body></html>"""


class OzonAntibotStub:
    """Start with `with OzonAntibotStub() as stub:`; `stub.base_url` is the root."""

    def __init__(self, *, solve_challenge: bool = True) -> None:
        self._solve = solve_challenge
        self._lock = threading.Lock()
        self._valid_tokens: set[str] = set()
        self.issued_tokens: list[str] = []
        self.home_user_agents: list[str] = []
        self.reports: list[dict[str, str]] = []
        self.api_statuses: list[int] = []
        self.api_requests: list[dict[str, str]] = []
        self.echo_requests: list[dict[str, str]] = []
        self.trap_hits = 0
        self.big_bytes_sent = 0
        self.home_hit = threading.Event()
        self.slow_started = threading.Event()
        self.release_slow = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(
                self,
                status: int,
                body: bytes,
                ctype: str = "text/plain",
                headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path, _, query = self.path.partition("?")
                params = dict(parse_qsl(query))
                try:
                    self._route(path, query, params)
                except BrokenPipeError, ConnectionResetError:
                    self.close_connection = True

            def _route(self, path: str, query: str, params: dict[str, str]) -> None:
                if path == "/":
                    self._home()
                elif path == "/report":
                    with stub._lock:
                        stub.reports.append(params)
                    self._send(200, b"ok")
                elif path == "/api":
                    self._api(query)
                elif path == "/redir":
                    self._send(307, b"redirect", headers={"Location": "/trap"})
                elif path == "/trap":
                    with stub._lock:
                        stub.trap_hits += 1
                    self._send(200, b"trap")
                elif path == "/big":
                    self._big(int(params.get("status", "403")))
                elif path == "/setcookie":
                    self._send(200, b"{}", headers={"Set-Cookie": "leak=LEAK-MARKER"})
                elif path == "/echo":
                    self._echo()
                elif path == "/slow":
                    stub.slow_started.set()
                    stub.release_slow.wait(30)
                    self._send(200, b"{}")
                elif path == "/body":
                    size = int(params["size"])
                    data = FIXTURE.read_bytes()
                    self._send(200, data + b" " * (size - len(data)))
                else:
                    self._send(404, b"")

            def _home(self) -> None:
                token = f"tok-{uuid.uuid4().hex}"
                with stub._lock:
                    stub.home_user_agents.append(self.headers.get("User-Agent", ""))
                    if stub._solve:
                        stub._valid_tokens.add(token)
                        stub.issued_tokens.append(token)
                set_token = (
                    "document.cookie = "
                    f'"__Secure-access-token={token}; Secure; path=/";'
                    if stub._solve
                    else ""
                )
                stub.home_hit.set()
                self._send(200, _PAGE.format(set_token=set_token).encode(), "text/html")

            def _api(self, query: str) -> None:
                cookie = self.headers.get("Cookie", "")
                pairs = dict(
                    item.split("=", 1) for item in cookie.split("; ") if "=" in item
                )
                with stub._lock:
                    valid = pairs.get("__Secure-access-token") in stub._valid_tokens
                    status = 200 if valid else 307
                    stub.api_statuses.append(status)
                    stub.api_requests.append(
                        {
                            "cookie": cookie,
                            "user_agent": self.headers.get("User-Agent", ""),
                            "query": query,
                        }
                    )
                if status == 200:
                    self._send(200, FIXTURE.read_bytes(), "application/json")
                else:
                    self._send(307, b"", headers={"Location": f"/api?{query}&__rr=1"})

            def _big(self, status: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(BIG_BODY_BYTES))
                self.end_headers()
                sent = 0
                try:
                    while sent < BIG_BODY_BYTES:
                        self.wfile.write(b"x" * _CHUNK)
                        self.wfile.flush()
                        sent += _CHUNK
                        with stub._lock:
                            stub.big_bytes_sent += _CHUNK
                finally:
                    self.close_connection = True

            def _echo(self) -> None:
                record = {
                    "cookie": self.headers.get("Cookie", ""),
                    "user_agent": self.headers.get("User-Agent", ""),
                    "path": self.path,
                }
                with stub._lock:
                    stub.echo_requests.append(record)
                self._send(200, json.dumps(record).encode(), "application/json")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.handle_error = lambda request, client_address: None  # type: ignore[method-assign]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def revoke_all_tokens(self) -> None:
        """Make every token issued so far spent (the next `/api` gives `307`)."""
        with self._lock:
            self._valid_tokens.clear()

    def __enter__(self) -> OzonAntibotStub:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release_slow.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
