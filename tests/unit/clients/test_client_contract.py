"""Cross-client contract matrix for `WbClient` and `OzonClient` (PAB-071 AC9).

Both clients must put the same trigger into the same failure category
(`PROJECT.md` sections 2.6, 2.11). Expected categories are literals from the
ticket, not derived from either client. The one deliberate divergence, an
endless redirect loop, has its own explicit column: `WbClient` does not
follow redirects (a 3xx is `transport_error`), `OzonClient` follows them by
hand, up to `MAX_REDIRECTS` hops, and reads exhaustion as the anti-bot's
`blocked`.

Bodies are wholly synthetic and only need to trigger the branch. WB- and
Ozon-specific behaviour stays in `test_wb_client.py` / `test_ozon_client.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Callable
from typing import NamedTuple

import httpx
import pytest

from app.schemas.marketplace import FetchFailureReason as R
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.base_client import BaseMarketplaceClient
from app.services.ozon_client import OzonClient
from app.services.wb_client import WbClient

_Handler = Callable[[httpx.Request], httpx.Response]
_ClientClass = type[WbClient] | type[OzonClient]


class _Case(NamedTuple):
    id: str
    make_handler: Callable[[_ClientClass], _Handler]
    wb: R
    ozon: R
    short_deadline: bool = False


def _status(code: int) -> Callable[[_ClientClass], _Handler]:
    def make(cls: _ClientClass) -> _Handler:
        return lambda request: httpx.Response(code)

    return make


def _raising(cls: _ClientClass) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("reset")

    return handler


def _stalled_body(cls: _ClientClass) -> _Handler:
    never = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b"{"
        await never.wait()

    return lambda request: httpx.Response(200, content=body())


def _oversized_body(cls: _ClientClass) -> _Handler:
    size = cls.MAX_BODY_BYTES + 1
    return lambda request: httpx.Response(200, content=b" " * size)


def _not_json(cls: _ClientClass) -> _Handler:
    return lambda request: httpx.Response(200, content=b"<html>not json</html>")


def _redirect_loop(cls: _ClientClass) -> _Handler:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": f"{request.url}&__rr={calls}"})

    return handler


_CASES = [
    _Case("403", _status(403), R.blocked, R.blocked),
    _Case("429", _status(429), R.blocked, R.blocked),
    _Case("404", _status(404), R.not_found, R.not_found),
    _Case("500", _status(500), R.transport_error, R.transport_error),
    _Case("connection-drop", _raising, R.transport_error, R.transport_error),
    _Case("deadline", _stalled_body, R.transport_error, R.transport_error, True),
    _Case("body-over-threshold", _oversized_body, R.bad_payload, R.bad_payload),
    _Case("non-json-200", _not_json, R.bad_payload, R.bad_payload),
    # The one explicit divergence: see the module docstring.
    _Case("redirect-loop", _redirect_loop, R.transport_error, R.blocked),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
@pytest.mark.parametrize("client_class", [WbClient, OzonClient], ids=["wb", "ozon"])
async def test_same_trigger_gives_the_expected_category(
    case: _Case, client_class: _ClientClass, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case.short_deadline:
        monkeypatch.setattr(client_class, "TIMEOUT_SECONDS", 0.05)
    expected = case.wb if client_class is WbClient else case.ozon
    article = 860043555 if client_class is WbClient else 3593896354

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(case.make_handler(client_class))
    ) as http:
        client: BaseMarketplaceClient = client_class(http)
        result = await asyncio.wait_for(client.get_product_data(article), timeout=10)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is expected


def test_only_the_redirect_loop_diverges_between_clients() -> None:
    """Guard of the matrix itself: a second divergence must be a conscious,
    reviewed edit of this test, not a quiet column change."""
    assert [c.id for c in _CASES if c.wb is not c.ozon] == ["redirect-loop"]
