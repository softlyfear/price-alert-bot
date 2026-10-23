"""Cross-client contract matrix for `WbClient` and `OzonClient` (PAB-071 AC9).

Both clients must put the same trigger into the same failure category
(`PROJECT.md` sections 2.6, 2.11, 2.14). Expected categories are literals from
the ticket, not derived from either client. The deliberate divergences have
their own explicit columns: `498` is WB's spent anti-bot token (a WB-only
status), and an endless `307` is `transport_error` for WB (it follows no
redirect) but `blocked` for Ozon (a `3xx` is the refusal of access, PAB-110).

WB is driven through `httpx.MockTransport`, Ozon through `FakeOzonSession`
(`OzonClient` no longer uses `httpx`). A body over the size cap lives in the
Ozon transport, so it is proved on the real `curl_cffi` transport in
`tests/integration/test_ozon_transport_live.py`, not here.

Bodies are wholly synthetic and only need to trigger the branch. WB- and
Ozon-specific behaviour stays in `test_wb_client.py` / `test_ozon_client.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

from app.schemas.marketplace import FetchFailureReason as R
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonReply
from app.services.wb_client import WbClient
from tests.unit.ozon_stubs import HANG
from tests.unit.ozon_stubs import FakeOzonSession
from tests.unit.ozon_stubs import Plan
from tests.unit.ozon_stubs import StubOzonAccess
from tests.unit.wb_access_stub import StubWbAccess

_Handler = Callable[[httpx.Request], httpx.Response]


class _Case(NamedTuple):
    id: str
    wb_handler: Callable[[], _Handler]
    # `None`: the trigger does not exist on the Ozon fake (see the docstring).
    ozon_plan: Plan | None
    wb: R
    ozon: R
    short_deadline: bool = False


def _status(code: int) -> Callable[[], _Handler]:
    return lambda: lambda request: httpx.Response(code)


def _raising() -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("reset")

    return handler


def _stalled_body() -> _Handler:
    never = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b"{"
        await never.wait()

    return lambda request: httpx.Response(200, content=body())


def _oversized_body() -> _Handler:
    size = WbClient.MAX_BODY_BYTES + 1
    return lambda request: httpx.Response(200, content=b" " * size)


def _not_json() -> _Handler:
    return lambda request: httpx.Response(200, content=b"<html>not json</html>")


def _redirect_loop() -> _Handler:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": f"{request.url}&__rr={calls}"})

    return handler


def _ozon(status: int) -> Plan:
    return OzonReply(status, None)


_CASES = [
    _Case("403", _status(403), _ozon(403), R.blocked, R.blocked),
    _Case("429", _status(429), _ozon(429), R.blocked, R.blocked),
    _Case("404", _status(404), _ozon(404), R.not_found, R.not_found),
    _Case("500", _status(500), _ozon(500), R.transport_error, R.transport_error),
    _Case(
        "connection-drop",
        _raising,
        CurlConnectionError("reset"),
        R.transport_error,
        R.transport_error,
    ),
    _Case("deadline", _stalled_body, HANG, R.transport_error, R.transport_error, True),
    _Case("body-over-threshold", _oversized_body, None, R.bad_payload, R.bad_payload),
    _Case(
        "non-json-200",
        _not_json,
        OzonReply(200, b"<html>not json</html>"),
        R.bad_payload,
        R.bad_payload,
    ),
    # Explicit divergences: see the module docstring.
    _Case("redirect-loop", _redirect_loop, _ozon(307), R.transport_error, R.blocked),
    _Case("498-always", _status(498), _ozon(498), R.blocked, R.transport_error),
]
_OZON_CASES = [c for c in _CASES if c.ozon_plan is not None]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
async def test_wb_trigger_gives_the_expected_category(
    case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case.short_deadline:
        monkeypatch.setattr(WbClient, "TIMEOUT_SECONDS", 0.05)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(case.wb_handler())
    ) as http:
        client = WbClient(http, StubWbAccess())
        result = await asyncio.wait_for(client.get_product_data(860043555), timeout=10)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is case.wb


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _OZON_CASES, ids=[c.id for c in _OZON_CASES])
async def test_ozon_trigger_gives_the_expected_category(
    case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert case.ozon_plan is not None
    if case.short_deadline:
        monkeypatch.setattr(OzonClient, "TIMEOUT_SECONDS", 0.05)
    client = OzonClient(FakeOzonSession(case.ozon_plan), StubOzonAccess())

    result = await asyncio.wait_for(client.get_product_data(3593896354), timeout=10)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is case.ozon


def test_only_the_named_cells_diverge_between_clients() -> None:
    """Guard of the matrix itself: a further divergence must be a conscious,
    reviewed edit of this test, not a quiet column change."""
    assert [c.id for c in _CASES if c.wb is not c.ozon] == [
        "redirect-loop",
        "498-always",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _OZON_CASES, ids=[c.id for c in _OZON_CASES])
async def test_ozon_batch_default_gives_each_article_the_single_path_category(
    case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAB-081 AC1: ``OzonClient`` has no batch API and inherits the default;
    the same trigger puts every requested article into the Ozon column's
    category (literal from the matrix, not recomputed)."""
    assert case.ozon_plan is not None
    if case.short_deadline:
        monkeypatch.setattr(OzonClient, "TIMEOUT_SECONDS", 0.05)
    client = OzonClient(FakeOzonSession(case.ozon_plan), StubOzonAccess())

    results = await asyncio.wait_for(
        client.get_products_data([3593896354, 1]), timeout=10
    )

    assert set(results) == {3593896354, 1}
    for result in results.values():
        assert isinstance(result, MarketplaceFetchFailure)
        assert result.reason is case.ozon


def test_ozon_batch_size_is_one_and_wb_is_one_hundred() -> None:
    """PAB-081: Ozon keeps one article per request, WB takes a hundred."""
    assert OzonClient.batch_size == 1
    assert WbClient.batch_size == 100


def _ozon_fixture() -> bytes:
    path = Path(__file__).resolve().parent.parent.parent / "fixtures" / "ozon"
    return (path / "entrypoint_3593896354.json").read_bytes()


@pytest.mark.asyncio
async def test_ozon_batch_default_on_the_real_fixture_is_one_request_per_article() -> (
    None
):
    """PAB-081 AC1: the real capture (page price 495 rub, no Ozon card) through the
    inherited default; the repeated article is requested once."""
    session = FakeOzonSession(OzonReply(200, _ozon_fixture()))
    client: BaseMarketplaceClient = OzonClient(session, StubOzonAccess())

    results = await client.get_products_data([3593896354, 3593896354])

    assert len(session.requests) == 1
    assert results == {
        3593896354: MarketplaceProductData(
            name="Лонгслив тельняшка длинный рукав 1 шт", price=49500, card_price=47100
        )
    }
