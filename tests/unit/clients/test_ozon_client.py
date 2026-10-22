"""Unit tests for app.services.ozon_client.OzonClient (PAB-071, PAB-110).

The transport is replaced by `FakeOzonSession` and the access by
`StubOzonAccess` (`tests/unit/ozon_stubs.py`); no network, no browser. The
real `curl_cffi` transport is proved against a local server in
`tests/integration/test_ozon_transport_live.py`. The success body is the real
customer-supplied slice `tests/fixtures/ozon/entrypoint_3593896354.json` fed
byte-for-byte (`PROJECT.md` sections 2.11, 8.2). Every other body is derived
from it by `_body` (decode the widget states, edit one, re-encode) or is wholly
synthetic, and is marked as such; none claims more about the Ozon contract
than `PROJECT.md` section 2.11.

Expected categories come from the status map of `PROJECT.md` section 2.14 O4
(ticket PAB-110 AC7), not from the code: `3xx`/`403` renew the access once,
then `blocked`; `429` is `blocked` without a renewal; `404` is the only
`not_found`; other statuses are `transport_error`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import ProxyError
from loguru import logger

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_access import OzonAccessUnavailable
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonReply
from tests.unit.ozon_stubs import HANG
from tests.unit.ozon_stubs import FakeOzonSession
from tests.unit.ozon_stubs import Plan
from tests.unit.ozon_stubs import StubOzonAccess
from tests.unit.ozon_stubs import make_access

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


class _Raw(NamedTuple):
    """A widget-state value written to the body as-is (not re-encoded)."""

    text: str


def _capture_records() -> tuple[list[Record], int]:
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(_sink, level=0)


class _Run(NamedTuple):
    result: MarketplaceFetchResult
    records: list[Record]
    session: FakeOzonSession
    access: StubOzonAccess


async def _run(
    *plans: Plan,
    access: StubOzonAccess | None = None,
    article: int = _ARTICLE,
) -> _Run:
    """One `get_product_data` on a planned transport, with the log captured."""
    session = FakeOzonSession(*plans)
    provider = access or StubOzonAccess()
    records, sink_id = _capture_records()
    try:
        result = await OzonClient(session, provider).get_product_data(article)
    finally:
        logger.remove(sink_id)
    return _Run(result, records, session, provider)


async def _fetch(
    *plans: Plan, article: int = _ARTICLE
) -> tuple[MarketplaceFetchResult, list[Record]]:
    run = await _run(*plans, article=article)
    return run.result, run.records


def _ok(body: bytes) -> OzonReply:
    return OzonReply(200, body)


def _status_only(status: int) -> OzonReply:
    """A non-2xx reply: the transport never reads its body (`body is None`)."""
    return OzonReply(status, None)


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


# --- success and the request ------------------------------------------------


@pytest.mark.asyncio
async def test_success_on_fixture_returns_name_and_price_without_card(
    fixture_bytes: bytes,
) -> None:
    """Page prices: 495 (no Ozon card), 471 (card), 1 742 (crossed out). Only
    495 is tracked; `cardPrice` instead of `price` would give 47100."""
    run = await _run(_ok(fixture_bytes))

    assert run.result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)
    assert run.records == []
    assert len(run.session.requests) == 1
    assert run.access.invalidated == []


@pytest.mark.asyncio
async def test_request_carries_url_parameter_access_and_bounds(
    fixture_bytes: bytes,
) -> None:
    run = await _run(_ok(fixture_bytes))

    url, params, access, timeout, max_body = run.session.requests[0]
    assert url == _URL
    assert params == {"url": f"/product/{_ARTICLE}/"}
    assert access == make_access(1)
    assert timeout == 10.0
    assert max_body == 4_194_304


# --- status map (PAB-110 AC7) -------------------------------------------------


class _Cell(NamedTuple):
    id: str
    plans: tuple[Plan, ...]
    reason: FetchFailureReason
    detail: str
    requests: int
    invalidations: int


_CELLS = [
    _Cell("429", (_status_only(429),), FetchFailureReason.blocked, "HTTP 429", 1, 0),
    _Cell("404", (_status_only(404),), FetchFailureReason.not_found, "HTTP 404", 1, 0),
    _Cell(
        "500",
        (_status_only(500),),
        FetchFailureReason.transport_error,
        "HTTP 500",
        1,
        0,
    ),
    _Cell(
        "307-307",
        (_status_only(307), _status_only(307)),
        FetchFailureReason.blocked,
        "HTTP 307",
        2,
        1,
    ),
    _Cell(
        "403-403",
        (_status_only(403), _status_only(403)),
        FetchFailureReason.blocked,
        "HTTP 403",
        2,
        1,
    ),
    _Cell(
        "403-then-429",
        (_status_only(403), _status_only(429)),
        FetchFailureReason.blocked,
        "HTTP 429",
        2,
        1,
    ),
    _Cell(
        "403-then-404",
        (_status_only(403), _status_only(404)),
        FetchFailureReason.not_found,
        "HTTP 404",
        2,
        1,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", _CELLS, ids=[c.id for c in _CELLS])
async def test_status_cell_gives_the_outcome_request_count_and_invalidations(
    cell: _Cell,
) -> None:
    run = await _run(*cell.plans)

    assert run.result == MarketplaceFetchFailure(reason=cell.reason, detail=cell.detail)
    assert len(run.session.requests) == cell.requests
    assert len(run.access.invalidated) == cell.invalidations


@pytest.mark.asyncio
async def test_persistent_refusal_costs_one_access_acquisition_per_article() -> None:
    """Two articles under a standing `307`: the second refusal of each article
    does not invalidate the renewed access: 2 renewals (not 4) for both."""
    access = StubOzonAccess()
    session = FakeOzonSession(_status_only(307))
    client = OzonClient(session, access)

    first = await client.get_product_data(_ARTICLE)
    second = await client.get_product_data(_ARTICLE + 1)

    blocked = MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="HTTP 307"
    )
    assert (first, second) == (blocked, blocked)
    assert len(session.requests) == 4
    assert len(access.invalidated) == 2
    assert len(access.issued) == 3  # the first access and one renewal per article


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [307, 301, 302, 308, 403], ids=str)
async def test_refusal_then_200_returns_data_with_the_new_access(
    first: int, fixture_bytes: bytes
) -> None:
    """The second request carries the cookies of the NEW access (the stub
    issues access 2 after `invalidate`), the first one those of access 1."""
    run = await _run(_status_only(first), _ok(fixture_bytes))

    assert run.result == MarketplaceProductData(name=_NAME, price=_PRICE_KOPECKS)
    assert [r[2] for r in run.session.requests] == [make_access(1), make_access(2)]
    assert run.access.invalidated == [make_access(1)]
    assert run.access.get_calls == 2
    assert [r["level"].name for r in run.records] == ["INFO"]


@pytest.mark.asyncio
async def test_third_request_is_never_made_after_two_refusals() -> None:
    run = await _run(_status_only(403), _status_only(403), _ok(b"{}"))

    assert len(run.session.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", list(OzonAccessStage), ids=lambda s: s.value)
async def test_access_refusal_is_blocked_without_any_request(
    stage: OzonAccessStage,
) -> None:
    access = StubOzonAccess(OzonAccessUnavailable(stage, "SECRET-DETAIL-MARKER"))

    run = await _run(_ok(b"{}"), access=access)

    assert run.result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked,
        detail=f"Ozon access unavailable: {stage}",
    )
    assert run.session.requests == []
    assert run.access.invalidated == []
    assert [r["level"].name for r in run.records] == ["WARNING"]
    assert "SECRET-DETAIL-MARKER" not in run.records[0]["message"]


@pytest.mark.asyncio
async def test_refusal_then_access_refusal_is_blocked_after_one_request() -> None:
    access = StubOzonAccess(
        None, OzonAccessUnavailable(OzonAccessStage.paused, "pause")
    )

    run = await _run(_status_only(307), access=access)

    assert run.result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked,
        detail="Ozon access unavailable: paused",
    )
    assert len(run.session.requests) == 1
    assert len(run.access.invalidated) == 1


@pytest.mark.asyncio
async def test_refusal_logs_info_for_renewal_then_warning_for_second_refusal() -> None:
    run = await _run(_status_only(403), _status_only(403))

    assert [r["level"].name for r in run.records] == ["INFO", "WARNING"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_blocked_body_never_reaches_detail_or_log(status: int) -> None:
    """A refusal reply carries no body at all (`None`), so a challenge text
    cannot leak; `detail` is the status literal."""
    result, records = await _fetch(_status_only(status), _status_only(status))

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail == f"HTTP {status}"
    assert all("incident" not in r["message"].lower() for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "detail"),
    [
        (CurlConnectionError("refused SECRET-EXC-MARKER"), "ConnectionError"),
        (ProxyError("proxy SECRET-EXC-MARKER"), "ProxyError"),
    ],
    ids=["connection-error", "proxy-error"],
)
async def test_curl_errors_are_transport_error_named_by_class(
    exc: Exception, detail: str
) -> None:
    run = await _run(exc)

    assert run.result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail=detail
    )
    assert [r["level"].name for r in run.records] == ["WARNING"]
    assert "SECRET-EXC-MARKER" not in run.records[0]["message"]
    assert run.access.invalidated == []
    assert len(run.session.requests) == 1


@pytest.mark.asyncio
async def test_stalled_attempt_hits_the_deadline_as_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OzonClient, "TIMEOUT_SECONDS", 0.05)

    run = await asyncio.wait_for(_run(HANG), timeout=5)

    assert run.result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail="TimeoutError"
    )
    assert [r["level"].name for r in run.records] == ["WARNING"]
    assert run.access.invalidated == []


@pytest.mark.asyncio
async def test_cancellation_propagates_and_is_not_swallowed() -> None:
    session = FakeOzonSession(HANG)
    client = OzonClient(session, StubOzonAccess())
    task = asyncio.create_task(client.get_product_data(_ARTICLE))
    await asyncio.wait_for(session.started.wait(), timeout=5)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


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
    result, records = await _fetch(_ok(body))

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
    result, _ = await _fetch(_ok(_with_title(title)))

    if ok:
        assert result == MarketplaceProductData(name=title, price=_PRICE_KOPECKS)
    else:
        assert isinstance(result, MarketplaceFetchFailure)
        assert result.reason is FetchFailureReason.bad_payload


@pytest.mark.asyncio
async def test_price_at_int32_limit_is_accepted() -> None:
    """21 474 836 rubles = 2 147 483 600 kopecks, the largest ruble amount
    under 2 147 483 647 kopecks."""
    result, _ = await _fetch(_ok(_with_price(f"21{_T}474{_T}836{_T}{_RUB}")))

    assert result == MarketplaceProductData(name=_NAME, price=2_147_483_600)


@pytest.mark.asyncio
async def test_unknown_extra_widgets_and_keys_do_not_break_parsing() -> None:
    """Synthetic: an extra widget and an extra key inside a read state."""

    def edit(d: dict[str, Any]) -> None:
        d["webSomethingNew-1-default-1"] = {"anything": [1, 2, 3]}
        d[_PRICE_KEY]["synthetic_extra"] = {"x": 1}

    result, _ = await _fetch(_ok(_body(edit)))

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
    result, records = await _fetch(_ok(body))

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail == _VALIDATION
    assert len(records) == 1
    assert "SECRET-MARKER" not in records[0]["message"]
    assert "SECRET-MARKER" not in str(records[0]["extra"])


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
    result, records = await _fetch(_ok(_body(drop=drop)))

    assert result == _failure(FetchFailureReason.bad_payload, detail)
    assert [r["level"].name for r in records] == ["ERROR"]
    assert detail in records[0]["message"]
    assert "3121879" not in records[0]["message"]
