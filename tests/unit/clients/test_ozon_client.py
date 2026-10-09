"""Unit tests for app.services.ozon_client.OzonClient (PAB-071).

The network is replaced by `httpx.MockTransport` in every test
(`_client_with` is the single place that builds an `AsyncClient`). The success
body is the real customer-supplied slice
`tests/fixtures/ozon/entrypoint_3593896354.json` fed byte-for-byte
(`PROJECT.md` sections 2.11, 8.2). Every other body is derived from it by
`_body` (decode the widget states, edit one, re-encode) or is wholly
synthetic, and is marked as such; none claims more about the Ozon contract
than `PROJECT.md` section 2.11.

Expected categories come from the ticket's status map, not from the code:
403/429 and an exhausted redirect chain are `blocked` (never `not_found`),
404 is the only `not_found`, everything else non-2xx is `transport_error`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple

import httpx
import pytest
from loguru import logger

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.services.ozon_client import OzonClient
from app.services.wb_client import WbClient

_ARTICLE = 3593896354
_NAME = "Лонгслив тельняшка длинный рукав 1 шт"
_PRICE_KOPECKS = 49500
_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "ozon"
    / "entrypoint_3593896354.json"
)
_T = "\u2009"
_RUB = "\u20bd"
_URL = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
_PRICE_KEY = "webPrice-3121879-default-1"
_HEADING_KEY = "webProductHeading-3385933-default-1"
_SKU_KEY = "webDetailSKU-3385551-default-1"
_TRAP_KEY = "webPriceDecreasedCompact-3156513-default-1"

_Handler = Callable[[httpx.Request], httpx.Response]


class _Raw(NamedTuple):
    """A widget-state value written to the body as-is (not re-encoded)."""

    text: str


def _client_with(handler: _Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _body_response(status_code: int, body: bytes) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return handler


def _capture_records() -> tuple[list[Record], int]:
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(_sink, level=0)


async def _fetch(
    handler: _Handler, article: int = _ARTICLE
) -> tuple[MarketplaceFetchResult, list[Record]]:
    """Run one call on a mock transport, returning the result and the log."""
    records, sink_id = _capture_records()
    try:
        async with _client_with(handler) as client:
            result = await OzonClient(client).get_product_data(article)
    finally:
        logger.remove(sink_id)
    return result, records


@pytest.fixture(scope="module")
def fixture_bytes() -> bytes:
    return _FIXTURE_PATH.read_bytes()


def _body(
    edit: Callable[[dict[str, Any]], None] | None = None,
    *,
    drop: tuple[str, ...] = (),
) -> bytes:
    """The fixture with its widget states decoded, edited and re-encoded.

    `edit` receives `{key: decoded_state}`; assigning a `_Raw` writes that
    text verbatim, any other value is JSON-encoded. Unedited states keep
    their content, so a derived body differs from the real one only where
    the test says so.
    """
    envelope = json.loads(_FIXTURE_PATH.read_bytes())
    decoded: dict[str, Any] = {
        k: json.loads(v) for k, v in envelope["widgetStates"].items()
    }
    for key in drop:
        del decoded[key]
    if edit is not None:
        edit(decoded)
    envelope["widgetStates"] = {
        k: v.text if isinstance(v, _Raw) else json.dumps(v, ensure_ascii=False)
        for k, v in decoded.items()
    }
    return json.dumps(envelope, ensure_ascii=False).encode()


def _with_price(text: Any) -> bytes:
    def edit(d: dict[str, Any]) -> None:
        d[_PRICE_KEY]["price"] = text

    return _body(edit)


def _with_title(text: Any) -> bytes:
    def edit(d: dict[str, Any]) -> None:
        d[_HEADING_KEY]["title"] = text

    return _body(edit)


def _failure(reason: FetchFailureReason, detail: str) -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(reason=reason, detail=detail)


# --- success ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_on_fixture_returns_name_and_price_without_card(
    fixture_bytes: bytes,
) -> None:
    """Page prices: 495 (no Ozon card), 471 (card), 1 742 (crossed out). Only
    495 is tracked; `cardPrice` instead of `price` would give 47100."""
    result, records = await _fetch(_body_response(200, fixture_bytes))

    assert result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)
    assert records == []


@pytest.mark.asyncio
async def test_request_is_get_with_url_parameter_and_single_call(
    fixture_bytes: bytes,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=fixture_bytes)

    await _fetch(handler)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.copy_with(query=None) == httpx.URL(_URL)
    assert request.url.params.multi_items() == [("url", f"/product/{_ARTICLE}/")]


# --- blocked ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_blocked_on_antibot_status_with_one_warning(status: int) -> None:
    """Synthetic challenge body shaped like `PROJECT.md` section 10 question 2."""
    challenge = {
        "blockURL": "https://example.invalid/block",
        "challengeURL": "https://example.invalid/challenge",
        "incidentId": "fab_chlg_synthetic",
        "supportURL": "https://example.invalid/support",
        "timeoutSec": 5,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=challenge)

    result, records = await _fetch(handler)

    assert result == _failure(FetchFailureReason.blocked, f"HTTP {status}")
    assert len(records) == 1
    assert records[0]["level"].name == "WARNING"
    assert "fab_chlg_synthetic" not in records[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 404, 500, 503, 400, 401])
async def test_non_2xx_body_is_never_read(status: int) -> None:
    yielded = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal yielded
        yielded += 1
        yield b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body())

    await _fetch(handler)

    assert yielded == 0


# --- other categories ------------------------------------------------------


@pytest.mark.asyncio
async def test_404_is_the_only_not_found_with_a_warning() -> None:
    result, records = await _fetch(_body_response(404, b""))

    assert result == _failure(FetchFailureReason.not_found, "HTTP 404")
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 500, 503])
async def test_other_statuses_are_transport_error(status: int) -> None:
    result, records = await _fetch(_body_response(status, b""))

    assert result == _failure(FetchFailureReason.transport_error, f"HTTP {status}")
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("abort"),
    ],
    ids=["connect-error", "connect-timeout", "read-timeout", "protocol-abort"],
)
async def test_httpx_errors_are_transport_error_named_by_class(
    exc: httpx.HTTPError,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    result, records = await _fetch(handler)

    assert result == _failure(FetchFailureReason.transport_error, type(exc).__name__)
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
async def test_connection_drop_mid_body_is_transport_error() -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b'{"widgetStates":'
        raise httpx.ReadError("reset")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    result, records = await _fetch(handler)

    assert result == _failure(FetchFailureReason.transport_error, "ReadError")
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
async def test_stalled_body_hits_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer 5 s fuse turns a missing deadline into a failure, not a hang."""
    monkeypatch.setattr(OzonClient, "TIMEOUT_SECONDS", 0.05)
    never = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'{"widgetStates":'
        await never.wait()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    result, records = await asyncio.wait_for(_fetch(handler), timeout=5)

    assert result == _failure(FetchFailureReason.transport_error, "TimeoutError")
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
async def test_cancellation_propagates_and_is_not_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _fetch(handler)


# --- bad_payload -----------------------------------------------------------


def _duplicate_price(d: dict[str, Any]) -> None:
    d["webPrice-999-default-1"] = d[_PRICE_KEY]


def _foreign_sku(d: dict[str, Any]) -> None:
    d[_SKU_KEY]["copyText"] = "1234567"


def _sku_not_string(d: dict[str, Any]) -> None:
    d[_SKU_KEY]["copyText"] = _ARTICLE


def _sku_without_copy_text(d: dict[str, Any]) -> None:
    del d[_SKU_KEY]["copyText"]


def _unavailable(d: dict[str, Any]) -> None:
    d[_PRICE_KEY]["isAvailable"] = False


def _available_as_string(d: dict[str, Any]) -> None:
    d[_PRICE_KEY]["isAvailable"] = "true"


def _price_state_not_json(d: dict[str, Any]) -> None:
    d[_PRICE_KEY] = _Raw("{not json")


def _price_state_not_a_string(d: dict[str, Any]) -> None:
    d[_PRICE_KEY] = _Raw("null")


def _no_heading_title(d: dict[str, Any]) -> None:
    del d[_HEADING_KEY]["title"]


_VALIDATION = "ValidationError"

_BAD_PAYLOAD_CELLS: list[tuple[str, bytes, str]] = [
    ("not-json", b"<html>captcha</html>", _VALIDATION),
    ("empty-body", b"", _VALIDATION),
    ("top-level-array", b"[]", _VALIDATION),
    ("no-widget-states", b'{"layout": []}', _VALIDATION),
    ("widget-state-not-a-string", b'{"widgetStates": {"webPrice-1": 5}}', _VALIDATION),
    ("price-state-not-json", _body(_price_state_not_json), _VALIDATION),
    ("price-state-json-null", _body(_price_state_not_a_string), _VALIDATION),
    ("two-price-widgets", _body(_duplicate_price), "webPrice ambiguous"),
    (
        "price-missing-only-decreased-compact-remains",
        _body(drop=(_PRICE_KEY,)),
        "webPrice missing",
    ),
    (
        "heading-widget-missing",
        _body(drop=(_HEADING_KEY,)),
        "webProductHeading missing",
    ),
    ("sku-widget-missing", _body(drop=(_SKU_KEY,)), "webDetailSKU missing"),
    ("sku-of-another-article", _body(_foreign_sku), "article mismatch"),
    ("sku-copy-text-not-string", _body(_sku_not_string), _VALIDATION),
    ("sku-without-copy-text", _body(_sku_without_copy_text), _VALIDATION),
    ("is-available-false", _body(_unavailable), "isAvailable is false"),
    ("is-available-string", _body(_available_as_string), _VALIDATION),
    ("title-missing", _body(_no_heading_title), _VALIDATION),
    ("price-without-group-separator", _with_price(f"1742{_T}{_RUB}"), _VALIDATION),
    ("price-nbsp", _with_price(f"495\u00a0{_RUB}"), _VALIDATION),
    ("price-unparseable", _with_price("по запросу"), _VALIDATION),
    ("price-zero", _with_price(f"0{_T}{_RUB}"), _VALIDATION),
    ("price-number", _with_price(495), _VALIDATION),
    ("price-over-int32", _with_price(f"21{_T}474{_T}837{_T}{_RUB}"), _VALIDATION),
    ("title-151-chars", _with_title("т" * 151), _VALIDATION),
    ("title-1-char", _with_title("т"), _VALIDATION),
    ("title-empty", _with_title(""), _VALIDATION),
    ("title-not-string", _with_title(37), _VALIDATION),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "detail"),
    [(b, d) for _id, b, d in _BAD_PAYLOAD_CELLS],
    ids=[i for i, _b, _d in _BAD_PAYLOAD_CELLS],
)
async def test_bad_payload_cell_is_marked_with_one_error_log(
    body: bytes, detail: str
) -> None:
    """Synthetic derivatives of the fixture (or wholly synthetic bodies)."""
    result, records = await _fetch(_body_response(200, body))

    assert result == _failure(FetchFailureReason.bad_payload, detail)
    assert [r["level"].name for r in records] == ["ERROR"]


def test_prefix_trap_cell_really_keeps_the_decreased_compact_widget() -> None:
    """Guard of the cell above: without `webPrice-*` the body still has the
    real `webPriceDecreasedCompact-*` key, whose segment is a different
    component."""
    widget_states = json.loads(_body(drop=(_PRICE_KEY,)))["widgetStates"]

    assert _TRAP_KEY in widget_states
    assert not any(k.startswith("webPrice-") for k in widget_states)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "ok"),
    [("тт", True), ("т" * 150, True), ("т", False), ("т" * 151, False)],
    ids=["2", "150", "1", "151"],
)
async def test_title_length_boundary_2_to_150(title: str, ok: bool) -> None:
    result, _ = await _fetch(_body_response(200, _with_title(title)))

    if ok:
        assert result == MarketplaceProductData(name=title, price=_PRICE_KOPECKS)
    else:
        assert isinstance(result, MarketplaceFetchFailure)
        assert result.reason is FetchFailureReason.bad_payload


@pytest.mark.asyncio
async def test_price_at_int32_limit_is_accepted() -> None:
    """21 474 836 rubles = 2 147 483 600 kopecks, the largest ruble amount
    under 2 147 483 647 kopecks."""
    result, _ = await _fetch(
        _body_response(200, _with_price(f"21{_T}474{_T}836{_T}{_RUB}"))
    )

    assert result == MarketplaceProductData(name=_NAME, price=2_147_483_600)


@pytest.mark.asyncio
async def test_unknown_extra_widgets_and_keys_do_not_break_parsing() -> None:
    """Synthetic: an extra widget and an extra key inside a read state."""

    def edit(d: dict[str, Any]) -> None:
        d["webSomethingNew-1-default-1"] = {"anything": [1, 2, 3]}
        d[_PRICE_KEY]["synthetic_extra"] = {"x": 1}

    result, _ = await _fetch(_body_response(200, _body(edit)))

    assert result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _with_price("SECRET-MARKER-495"),
        _with_title("SECRET-MARKER" + "т" * 200),
        b"SECRET-MARKER{",
    ],
    ids=["price", "title", "not-json"],
)
async def test_bad_payload_never_leaks_input_into_detail_or_log(body: bytes) -> None:
    """`str(e)` of a pydantic error echoes the input value; only the class
    name may reach `detail` and the log."""
    result, records = await _fetch(_body_response(200, body))

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail == _VALIDATION
    assert len(records) == 1
    assert "SECRET-MARKER" not in records[0]["message"]
    assert "SECRET-MARKER" not in str(records[0]["extra"])


# --- body size cap ---------------------------------------------------------


def _padded(fixture: bytes, size: int) -> bytes:
    assert len(fixture) <= size
    return fixture + b" " * (size - len(fixture))


def test_max_body_bytes_is_four_mebibytes() -> None:
    assert OzonClient.MAX_BODY_BYTES == 4_194_304


@pytest.mark.asyncio
async def test_body_of_exactly_max_bytes_still_parses(fixture_bytes: bytes) -> None:
    result, _ = await _fetch(_body_response(200, _padded(fixture_bytes, 4_194_304)))

    assert result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)


@pytest.mark.asyncio
async def test_body_one_byte_over_max_is_bad_payload_naming_the_threshold(
    fixture_bytes: bytes,
) -> None:
    result, records = await _fetch(
        _body_response(200, _padded(fixture_bytes, 4_194_305))
    )

    assert result == _failure(
        FetchFailureReason.bad_payload, "body exceeds 4194304 bytes"
    )
    assert [r["level"].name for r in records] == ["ERROR"]


# --- manual redirect loop (PAB-071 attempt 2, decision R4') -----------------

_MARKER = "LOCATION-MARKER-7f3a"


class _CountingStream(httpx.AsyncByteStream):
    """Response body that counts how many chunks were pulled from it."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk


def _oversized_chunks() -> list[bytes]:
    """Two chunks, together one byte over `OzonClient.MAX_BODY_BYTES`."""
    half = OzonClient.MAX_BODY_BYTES // 2
    return [b"x" * half, b"x" * (OzonClient.MAX_BODY_BYTES - half + 1)]


def test_oversized_chunks_really_exceed_the_body_cap() -> None:
    """Guard of the redirect cells: their 3xx bodies are over the cap."""
    assert sum(map(len, _oversized_chunks())) == OzonClient.MAX_BODY_BYTES + 1


@pytest.mark.asyncio
async def test_redirect_loop_is_blocked_and_3xx_bodies_are_never_read() -> None:
    """Synthetic: the observed anti-bot answer without cookies, an endless
    307 to the same address plus `&__rr=N`, each with an oversized body."""
    streams: list[_CountingStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stream = _CountingStream(_oversized_chunks())
        streams.append(stream)
        return httpx.Response(
            307,
            headers={"Location": f"{request.url}&__rr={len(streams)}"},
            stream=stream,
        )

    result, records = await _fetch(handler)

    assert result == _failure(FetchFailureReason.blocked, "more than 5 redirects")
    assert OzonClient.MAX_REDIRECTS == 5
    assert len(streams) == OzonClient.MAX_REDIRECTS + 1
    assert [s.pulled for s in streams] == [0] * 6
    assert [r["level"].name for r in records] == ["WARNING"]
    assert "__rr" not in records[0]["message"]


@pytest.mark.asyncio
async def test_body_counter_registers_reading_of_a_200_body(
    fixture_bytes: bytes,
) -> None:
    """Positive control of the zero-chunk assertions: the same counter does
    see a body that is read."""
    stream = _CountingStream([fixture_bytes])

    result, _ = await _fetch(lambda request: httpx.Response(200, stream=stream))

    assert result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)
    assert stream.pulled == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        f"{_URL}?url=/product/{_ARTICLE}/&__rr=1",
        f"/api/entrypoint-api.bx/page/json/v2?url=/product/{_ARTICLE}/&__rr=1",
        f"https://www.ozon.ru:443/api/entrypoint-api.bx/page/json/v2?url=/product/{_ARTICLE}/&__rr=1",
    ],
    ids=["absolute", "relative", "explicit-default-port"],
)
async def test_single_redirect_is_followed_without_reading_its_body(
    fixture_bytes: bytes, location: str
) -> None:
    redirect_stream = _CountingStream(_oversized_chunks())
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if len(seen) == 1:
            return httpx.Response(
                307, headers={"Location": location}, stream=redirect_stream
            )
        return httpx.Response(200, content=fixture_bytes)

    result, records = await _fetch(handler)

    assert result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)
    assert len(seen) == 2
    assert seen[1].params["__rr"] == "1"
    assert redirect_stream.pulled == 0
    assert records == []


@pytest.mark.asyncio
async def test_shared_client_configuration_is_unchanged_after_a_redirect_call(
    fixture_bytes: bytes,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "__rr" not in request.url.params:
            return httpx.Response(307, headers={"Location": f"{request.url}&__rr=1"})
        return httpx.Response(200, content=fixture_bytes)

    async with _client_with(handler) as client:
        limit_before = client.max_redirects
        assert client.follow_redirects is False
        result = await OzonClient(client).get_product_data(_ARTICLE)
        assert client.follow_redirects is False
        assert client.max_redirects == limit_before

    assert result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        f"https://evil.example/{_MARKER}?token={_MARKER}",
        f"http://www.ozon.ru/{_MARKER}?token={_MARKER}",
        f"https://www.ozon.ru:8443/{_MARKER}?token={_MARKER}",
    ],
    ids=["foreign-host", "downgrade-to-http", "foreign-port"],
)
async def test_redirect_to_foreign_origin_is_bad_payload_without_any_request(
    location: str,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": location})

    result, records = await _fetch(handler)

    assert _MARKER in location  # positive control for the leak assertions
    assert result == _failure(
        FetchFailureReason.bad_payload, "redirect left the Ozon origin"
    )
    assert len(seen) == 1
    assert seen[0].startswith(_URL)
    assert [r["level"].name for r in records] == ["ERROR"]
    assert _MARKER not in records[0]["message"]
    assert _MARKER not in str(records[0]["extra"])
    assert "evil.example" not in records[0]["message"]


@pytest.mark.asyncio
async def test_redirect_cookie_reaches_next_step_but_not_other_hosts() -> None:
    """Synthetic cookie. Step 1: `307` + `Set-Cookie`; step 2 carries it and is
    blocked (the factual chain of `PROJECT.md` section 2.11)."""
    cookie = "ozon_synthetic=SYNTH-COOKIE-1"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host != "www.ozon.ru":
            return httpx.Response(404)
        if len(requests) == 1:
            return httpx.Response(
                307,
                headers={
                    "Location": f"{request.url}&__rr=1",
                    "Set-Cookie": f"{cookie}; Secure; Path=/",
                },
            )
        return httpx.Response(403)

    async with _client_with(handler) as client:
        ozon_result = await OzonClient(client).get_product_data(_ARTICLE)
        wb_result = await WbClient(client).get_product_data(860043555)

    assert ozon_result == _failure(FetchFailureReason.blocked, "HTTP 403")
    ozon_requests = requests[:2]
    assert len(requests) == 3
    assert "cookie" not in ozon_requests[0].headers
    assert cookie in ozon_requests[1].headers["cookie"]  # positive control
    assert requests[2].url.host != "www.ozon.ru"
    assert isinstance(wb_result, MarketplaceFetchFailure)
    assert "cookie" not in requests[2].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirect_without_location_is_transport_error_and_body_unread(
    status: int,
) -> None:
    stream = _CountingStream([b"x" * 100])
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, stream=stream)

    result, records = await _fetch(handler)

    assert result == _failure(FetchFailureReason.transport_error, f"HTTP {status}")
    assert calls == 1
    assert stream.pulled == 0
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drop", "detail"),
    [
        ((_PRICE_KEY,), "webPrice missing"),
        ((_HEADING_KEY,), "webProductHeading missing"),
        ((_SKU_KEY,), "webDetailSKU missing"),
    ],
    ids=["price", "heading", "sku"],
)
async def test_missing_widget_is_named_in_detail_and_log_without_ids(
    drop: tuple[str, ...], detail: str
) -> None:
    result, records = await _fetch(_body_response(200, _body(drop=drop)))

    assert result == _failure(FetchFailureReason.bad_payload, detail)
    assert [r["level"].name for r in records] == ["ERROR"]
    assert detail in records[0]["message"]
    assert "3121879" not in records[0]["message"]
