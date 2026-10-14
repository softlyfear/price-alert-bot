"""A local stand-in for the Wildberries anti-bot flow (not WB, no network).

Served on `127.0.0.1` by the stdlib `http.server` in a thread, so a real
Chromium and a real `httpx` client can talk to it without any new dependency.

- `GET /` (the "home page"): answers `498` with a page whose script reports
  the browser's own view (`navigator.webdriver`, language, time zone) to
  `/report`, and only then sets the cookie `x_wbaas_token` to a token issued
  by this response. In `solve_challenge=False` mode the page sets no token
  cookie at all (an unsolved check). The request's `User-Agent` is recorded.
- `GET /api`: `498` without a currently valid token cookie, `403` with the
  cookie but without the `deviceid` header, `200` with the customer's
  captured response (`tests/fixtures/wb/detail_860043555.json`) with both.
"""

from __future__ import annotations

import threading
import uuid
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import TracebackType

_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "wb" / "detail_860043555.json"
)

_PAGE = """<!doctype html><html><body><script>
document.cookie = "other_cookie=1; path=/";
document.cookie = "x_wbaas_token=; path=/";
const q = new URLSearchParams({{
  webdriver: String(navigator.webdriver),
  language: navigator.language,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
}});
fetch("/report?" + q.toString()).finally(() => {{
  {set_token}
}});
</script></body></html>"""


class WbAntibotStub:
    """Start with `with WbAntibotStub() as stub:`; `stub.base_url` is the root."""

    def __init__(self, *, solve_challenge: bool = True) -> None:
        self._solve = solve_challenge
        self._lock = threading.Lock()
        self._valid_tokens: set[str] = set()
        self.issued_tokens: list[str] = []
        self.home_user_agents: list[str] = []
        self.reports: list[dict[str, str]] = []
        self.api_statuses: list[int] = []
        self.api_requests: list[dict[str, str]] = []
        self.home_hit = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path, _, query = self.path.partition("?")
                if path == "/":
                    self._home()
                elif path == "/report":
                    stub._record_report(query)
                    self._send(200, b"ok", "text/plain")
                elif path == "/api":
                    self._api()
                else:
                    self._send(404, b"", "text/plain")

            def _home(self) -> None:
                token = f"tok-{uuid.uuid4().hex}"
                with stub._lock:
                    stub.home_user_agents.append(self.headers.get("User-Agent", ""))
                    if stub._solve:
                        stub._valid_tokens.add(token)
                        stub.issued_tokens.append(token)
                set_token = (
                    f'document.cookie = "x_wbaas_token={token}; path=/";'
                    if stub._solve
                    else ""
                )
                stub.home_hit.set()
                self._send(498, _PAGE.format(set_token=set_token).encode(), "text/html")

            def _api(self) -> None:
                cookie = self.headers.get("Cookie", "")
                device_id = self.headers.get("deviceid")
                with stub._lock:
                    valid = any(
                        cookie == f"x_wbaas_token={t}" for t in stub._valid_tokens
                    )
                    if not valid:
                        status = 498
                    elif device_id is None:
                        status = 403
                    else:
                        status = 200
                    stub.api_statuses.append(status)
                    stub.api_requests.append(
                        {"cookie": cookie, "deviceid": device_id or ""}
                    )
                if status == 200:
                    self._send(200, _FIXTURE.read_bytes(), "application/json")
                else:
                    self._send(status, b"{}", "application/json")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _record_report(self, query: str) -> None:
        from urllib.parse import parse_qsl

        with self._lock:
            self.reports.append(dict(parse_qsl(query)))

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def revoke_all_tokens(self) -> None:
        """Make every token issued so far spent (the next `/api` gives `498`)."""
        with self._lock:
            self._valid_tokens.clear()

    def __enter__(self) -> WbAntibotStub:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
