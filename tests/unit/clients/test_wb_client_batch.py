"""Unit tests for ``WbClient.get_products_data`` (PAB-081 AC2).

The network is replaced by ``httpx.MockTransport``; the token provider is the
``StubWbAccess`` fake. The correct answer is built from the real
customer-supplied capture (``tests/fixtures/wb/detail_860043555.json``): every
product of a batch payload is a copy of its first product with another ``id``,
so only the fields a test varies differ from what WB really sent. Expected
values are literals or come from the table that built the payload, never from a
recomputation of the client's own logic.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

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
from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbAccessUnavailable
from app.services.wb_client import WbClient
from tests.unit.wb_access_stub import StubWbAccess
from tests.unit.wb_access_stub import make_pair

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "wb"
    / "detail_860043555.json"
)
_Handler = Callable[[httpx.Request], httpx.Response]


def _template() -> dict[str, Any]:
    """The real product of the capture: 35 keys, ``sizes[0]`` without a price."""
    payload = json.loads(_FIXTURE_PATH.read_bytes())
    product: dict[str, Any] = payload["products"][0]
    return product


def _product(article: int, price: int | None = 142900, name: str = "Товар") -> Any:
    """A copy of the real product with ``id``, ``name`` and one priced size."""
    product = copy.deepcopy(_template())
    product["id"] = article
    product["name"] = name
    product["sizes"] = [{}] if price is None else [{"price": {"product": price}}]
    return product


def _payload(*products: Any) -> bytes:
    return json.dumps({"products": list(products)}).encode()


def _client_with(handler: _Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _recording(
    responses: list[httpx.Response] | httpx.Response, seen: list[httpx.Request]
) -> _Handler:
    """Answer the n-th request with ``responses[n]`` (or always one response)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(responses, httpx.Response):
            return responses
        return responses[len(seen) - 1]

    return handler


def _capture() -> tuple[list[Record], int]:
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(_sink, level=0)


def _raw_nm(request: httpx.Request) -> str:
    """The ``nm`` value exactly as sent: ``request.url.query`` is the raw bytes."""
    pairs = [part.split(b"=", 1) for part in request.url.query.split(b"&")]
    values = [value for key, value in pairs if key == b"nm"]
    assert len(values) == 1
    return values[0].decode()


async def _fetch(
    handler: _Handler,
    articles: list[int],
    access: StubWbAccess | None = None,
) -> dict[int, MarketplaceFetchResult]:
    client = _client_with(handler)
    async with client:
        return await WbClient(client, access or StubWbAccess()).get_products_data(
            articles
        )


def _failure(reason: FetchFailureReason, detail: str) -> MarketplaceFetchFailure:
    return MarketplaceFetchFailure(reason=reason, detail=detail)


def test_batch_size_is_one_hundred() -> None:
    """AC2: the measured weight of one request (PROJECT.md 2.12, R13)."""
    assert WbClient.batch_size == 100


@pytest.mark.asyncio
async def test_hundred_articles_are_one_request_with_a_literal_semicolon_in_nm() -> (
    None
):
    """AC2(а). Mutations: ``params=`` instead of the hand-built query (``%3B``);
    a request per article; a separator other than ``;``."""
    articles = list(range(1001, 1101))
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    body = _payload(*(_product(a, price=a * 100) for a in articles))
    client = _client_with(_recording(httpx.Response(200, content=body), seen))
    async with client:
        results = await WbClient(client, access).get_products_data(articles)
        jar_size = len(client.cookies.jar)

    assert len(seen) == 1
    assert _raw_nm(seen[0]) == ";".join(str(a) for a in articles)
    assert b"%3B" not in seen[0].url.query.upper()
    assert seen[0].headers["cookie"] == "x_wbaas_token=TOKEN-MARKER-1"
    assert seen[0].headers["deviceid"] == make_pair(1).device_id
    assert jar_size == 0
    assert results == {
        a: MarketplaceProductData(name="Товар", price=a * 100) for a in articles
    }


@pytest.mark.asyncio
async def test_batch_request_keeps_the_single_request_parameters_and_address() -> None:
    """AC2(а): only ``nm`` differs from the single-article request."""
    seen: list[httpx.Request] = []
    await _fetch(_recording(httpx.Response(200, content=_payload()), seen), [5, 6])

    url = seen[0].url
    assert url.host == "www.wildberries.ru"
    assert url.path == "/__internal/u-card/cards/v4/detail"
    expected = {
        "appType": "1",
        "curr": "rub",
        "dest": "-1257786",
        "spp": "30",
        "hide_vflags": "4294967296",
        "hide_dflags": "1048576",
        "mtype": "257",
        "lang": "ru",
        "ab_testing": "false",
        "nm": "5;6",
    }
    assert sorted(url.params.multi_items()) == sorted(expected.items())


@pytest.mark.asyncio
async def test_250_articles_are_three_requests_of_100_100_and_50() -> None:
    """AC2(б). Mutations: chunk size off by one; the tail chunk dropped."""
    articles = list(range(1, 251))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        requested = [int(a) for a in _raw_nm(request).split(";")]
        return httpx.Response(200, content=_payload(*(_product(a) for a in requested)))

    results = await _fetch(handler, articles)

    chunks = [_raw_nm(r).split(";") for r in seen]
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert [int(a) for chunk in chunks for a in chunk] == articles
    assert set(results) == set(articles)
    assert all(isinstance(r, MarketplaceProductData) for r in results.values())


@pytest.mark.asyncio
async def test_repeated_articles_are_requested_once_in_first_seen_order() -> None:
    """AC2(б). Mutation: no ``dict.fromkeys`` (the repeat is requested twice)."""
    seen: list[httpx.Request] = []
    body = _payload(_product(30), _product(10), _product(20))
    results = await _fetch(
        _recording(httpx.Response(200, content=body), seen), [30, 10, 30, 20, 10]
    )

    assert len(seen) == 1
    assert _raw_nm(seen[0]) == "30;10;20"
    assert set(results) == {10, 20, 30}


@pytest.mark.asyncio
async def test_empty_input_makes_no_request_and_returns_an_empty_dict() -> None:
    """Edge: no articles, no chunk, no request, no ``get`` of the provider."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    results = await _fetch(
        _recording(httpx.Response(200, content=_payload()), seen), [], access
    )

    assert results == {}
    assert seen == []
    assert access.get_calls == 0


@pytest.mark.asyncio
async def test_prices_are_matched_by_id_when_products_come_in_reverse_order() -> None:
    """AC2(в). Mutation: positional matching (prices swap between articles)."""
    body = _payload(
        _product(333, price=30000, name="Третий"),
        _product(222, price=20000, name="Второй"),
        _product(111, price=10000, name="Первый"),
    )
    results = await _fetch(
        _recording(httpx.Response(200, content=body), []), [111, 222, 333]
    )

    assert results == {
        111: MarketplaceProductData(name="Первый", price=10000),
        222: MarketplaceProductData(name="Второй", price=20000),
        333: MarketplaceProductData(name="Третий", price=30000),
    }


@pytest.mark.asyncio
async def test_batch_of_the_real_fixture_product_gives_the_known_price() -> None:
    """AC2(г): the unmodified real product (``sizes[0]`` has no price) among
    others; ``142900`` is kopecks confirmed against the customer's UI."""
    real = _template()
    body = json.dumps({"products": [_product(7, price=500), real]}).encode()
    results = await _fetch(
        _recording(httpx.Response(200, content=body), []), [860043555, 7]
    )

    assert results[860043555] == MarketplaceProductData(
        name="Кроссовки женские на высокой подошве", price=142900
    )
    assert results[7] == MarketplaceProductData(name="Товар", price=500)


@pytest.mark.asyncio
async def test_product_without_any_priced_size_is_out_of_stock() -> None:
    """AC2(г). Mutation: ``out_of_stock`` -> ``bad_payload``, or the unpriced
    product dropping the priced neighbour."""
    body = _payload(_product(1, price=None), _product(2, price=9900))
    results = await _fetch(_recording(httpx.Response(200, content=body), []), [1, 2])

    assert results == {
        1: _failure(FetchFailureReason.out_of_stock, "no size has a price"),
        2: MarketplaceProductData(name="Товар", price=9900),
    }


@pytest.mark.asyncio
async def test_article_absent_from_a_non_empty_response_is_not_found() -> None:
    """AC2(г). Mutation: ``bad_payload`` (the single path's mark) or a missing key."""
    body = _payload(_product(1, price=9900))
    results = await _fetch(_recording(httpx.Response(200, content=body), []), [1, 2])

    assert results == {
        1: MarketplaceProductData(name="Товар", price=9900),
        2: _failure(FetchFailureReason.not_found, "absent from batch response"),
    }


@pytest.mark.asyncio
async def test_empty_products_is_not_found_for_every_article() -> None:
    """AC2(г). Mutation: ``bad_payload`` or only the first article marked."""
    results = await _fetch(
        _recording(httpx.Response(200, content=_payload()), []), [1, 2, 3]
    )

    empty = _failure(FetchFailureReason.not_found, "empty products")
    assert results == {1: empty, 2: empty, 3: empty}


@pytest.mark.asyncio
async def test_unrequested_products_in_the_response_are_ignored() -> None:
    """Edge: an extra product WB was not asked for does not become a key."""
    body = _payload(_product(1, price=100), _product(99, price=200))
    results = await _fetch(_recording(httpx.Response(200, content=body), []), [1])

    assert results == {1: MarketplaceProductData(name="Товар", price=100)}


@pytest.mark.asyncio
async def test_first_product_wins_when_the_response_repeats_an_id() -> None:
    """Same rule as the single path (first match by ``id``)."""
    body = _payload(_product(1, price=100), _product(1, price=999))
    results = await _fetch(_recording(httpx.Response(200, content=body), []), [1])

    assert results == {1: MarketplaceProductData(name="Товар", price=100)}


@pytest.mark.asyncio
async def test_one_malformed_product_is_bad_payload_for_that_article_only() -> None:
    """AC2: a name over the ``MarketplaceProductData`` limit (150) fails only
    its own article; the neighbour keeps its price."""
    body = _payload(_product(1, name="x" * 151), _product(2, price=9900))
    records, sink_id = _capture()
    try:
        results = await _fetch(
            _recording(httpx.Response(200, content=body), []), [1, 2]
        )
    finally:
        logger.remove(sink_id)

    assert results == {
        1: _failure(FetchFailureReason.bad_payload, "ValidationError"),
        2: MarketplaceProductData(name="Товар", price=9900),
    }
    assert [r["level"].name for r in records] == ["ERROR"]
    assert "xxxx" not in records[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        b'{"products": "nope"}',
        json.dumps({"products": [{"id": 1, "name": 424242, "sizes": []}]}).encode(),
        json.dumps({"products": [{"name": "no id", "sizes": []}]}).encode(),
    ],
    ids=["not_json", "products_not_a_list", "name_not_a_string", "product_without_id"],
)
async def test_changed_response_format_is_bad_payload_for_every_article_with_one_log(
    body: bytes,
) -> None:
    """AC2(д). Mutations: failure only for the first article; one record per
    article; ``str(e)`` in ``detail`` (it echoes ``424242``)."""
    records, sink_id = _capture()
    try:
        results = await _fetch(
            _recording(httpx.Response(200, content=body), []), [1, 2, 3]
        )
    finally:
        logger.remove(sink_id)

    expected = _failure(FetchFailureReason.bad_payload, "ValidationError")
    assert results == {1: expected, 2: expected, 3: expected}
    assert [r["level"].name for r in records] == ["ERROR"]
    assert "424242" not in records[0]["message"]


# --- request-level failures: same category and detail as the single path ----


def _raises(exc: httpx.HTTPError) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _oversized() -> httpx.Response:
    return httpx.Response(200, content=b" " * 4_194_305)


_FAILURE_CASES: dict[str, tuple[_Handler, FetchFailureReason, str, bool]] = {
    "403": (
        lambda r: httpx.Response(403),
        FetchFailureReason.blocked,
        "HTTP 403",
        True,
    ),
    "429": (
        lambda r: httpx.Response(429),
        FetchFailureReason.blocked,
        "HTTP 429",
        False,
    ),
    "404": (
        lambda r: httpx.Response(404),
        FetchFailureReason.not_found,
        "HTTP 404",
        False,
    ),
    "500": (
        lambda r: httpx.Response(500),
        FetchFailureReason.transport_error,
        "HTTP 500",
        False,
    ),
    "503": (
        lambda r: httpx.Response(503),
        FetchFailureReason.transport_error,
        "HTTP 503",
        False,
    ),
    "400": (
        lambda r: httpx.Response(400),
        FetchFailureReason.transport_error,
        "HTTP 400",
        False,
    ),
    "connect_timeout": (
        _raises(httpx.ConnectTimeout("t")),
        FetchFailureReason.transport_error,
        "ConnectTimeout",
        False,
    ),
    "read_error": (
        _raises(httpx.ReadError("r")),
        FetchFailureReason.transport_error,
        "ReadError",
        False,
    ),
    "oversized": (
        lambda r: _oversized(),
        FetchFailureReason.bad_payload,
        "body exceeds 4194304 bytes",
        False,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_FAILURE_CASES))
async def test_request_failure_marks_every_article_alike_with_one_log_record(
    case: str,
) -> None:
    """AC2(д). Mutations: a different category/detail for the batch; a record per
    article; the pair of a ``403`` kept (or of a non-``403`` dropped)."""
    handler, reason, detail, invalidates = _FAILURE_CASES[case]
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    access = StubWbAccess()
    records, sink_id = _capture()
    try:
        results = await _fetch(recording, [1, 2, 3], access)
    finally:
        logger.remove(sink_id)

    expected = _failure(reason, detail)
    assert results == {1: expected, 2: expected, 3: expected}
    assert len(seen) == 1
    assert access.get_calls == 1
    assert access.invalidated == ([make_pair(1)] if invalidates else [])
    assert len(records) == 1
    assert "batch of 3 articles" in records[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [c for c in _FAILURE_CASES if c != "oversized"])
async def test_request_failure_of_a_batch_equals_the_single_path_failure(
    case: str,
) -> None:
    """AC2(д), parity: the same response gives the single path the same result.

    ``oversized`` is excluded: the caps differ by design, so the ``detail`` names
    a different limit (see the dedicated test below)."""
    handler, _, _, _ = _FAILURE_CASES[case]
    single_client = _client_with(handler)
    async with single_client:
        single = await WbClient(single_client, StubWbAccess()).get_product_data(1)

    results = await _fetch(handler, [1, 2])

    assert results == {1: single, 2: single}


@pytest.mark.asyncio
async def test_oversized_body_is_bad_payload_on_both_paths_with_each_own_cap() -> None:
    """AC2(д): the same 4 MiB + 1 body is ``bad_payload`` for both paths; only the
    cap named in ``detail`` differs (1 MiB single, 4 MiB batch)."""
    handler = _FAILURE_CASES["oversized"][0]
    single_client = _client_with(handler)
    async with single_client:
        single = await WbClient(single_client, StubWbAccess()).get_product_data(1)
    batch = await _fetch(handler, [1])

    assert single == _failure(
        FetchFailureReason.bad_payload, "body exceeds 1048576 bytes"
    )
    assert batch == {
        1: _failure(FetchFailureReason.bad_payload, "body exceeds 4194304 bytes")
    }


@pytest.mark.asyncio
async def test_stalled_batch_hits_the_deadline_for_every_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2(д): timeout. ``TimeoutError`` for all, one WARNING."""
    monkeypatch.setattr(WbClient, "TIMEOUT_SECONDS", 0.05)
    never = asyncio.Event()

    async def body() -> Any:
        yield b'{"products":'
        await never.wait()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    records, sink_id = _capture()
    try:
        results = await asyncio.wait_for(_fetch(handler, [1, 2]), timeout=5)
    finally:
        logger.remove(sink_id)

    expected = _failure(FetchFailureReason.transport_error, "TimeoutError")
    assert results == {1: expected, 2: expected}
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
async def test_body_of_exactly_max_bytes_still_parses() -> None:
    """AC2(д), boundary. Mutation ``>`` -> ``>=`` in the cap."""
    body = _payload(_product(1, price=100))
    padded = body + b" " * (WbClient.MAX_BODY_BYTES - len(body))
    assert len(padded) == 1_048_576
    results = await _fetch(_recording(httpx.Response(200, content=padded), []), [1])

    assert results == {1: MarketplaceProductData(name="Товар", price=100)}


@pytest.mark.asyncio
async def test_batch_body_over_the_single_cap_still_parses() -> None:
    """AC2(и): a batch body of ``MAX_BODY_BYTES + 1`` is legal.
    Mutation: the single cap used on the batch path."""
    body = _payload(_product(1, price=100))
    padded = body + b" " * (1_048_577 - len(body))
    assert len(padded) == 1_048_577
    results = await _fetch(_recording(httpx.Response(200, content=padded), []), [1])

    assert results == {1: MarketplaceProductData(name="Товар", price=100)}


@pytest.mark.asyncio
async def test_batch_body_of_exactly_batch_max_bytes_still_parses() -> None:
    """AC2(и), boundary: exactly 4 MiB is accepted."""
    body = _payload(_product(1, price=100))
    padded = body + b" " * (WbClient.MAX_BATCH_BODY_BYTES - len(body))
    assert len(padded) == 4_194_304
    results = await _fetch(_recording(httpx.Response(200, content=padded), []), [1])

    assert results == {1: MarketplaceProductData(name="Товар", price=100)}


@pytest.mark.asyncio
async def test_batch_body_over_batch_max_is_bad_payload_for_all_with_one_error() -> (
    None
):
    """AC2(и): ``MAX_BATCH_BODY_BYTES + 1`` -> ``bad_payload`` for every article
    and one ``ERROR`` record. Mutation: the cap raised or removed."""
    body = _payload(_product(1, price=100), _product(2, price=200))
    padded = body + b" " * (4_194_305 - len(body))
    records, sink_id = _capture()
    try:
        results = await _fetch(
            _recording(httpx.Response(200, content=padded), []), [1, 2]
        )
    finally:
        logger.remove(sink_id)

    expected = _failure(FetchFailureReason.bad_payload, "body exceeds 4194304 bytes")
    assert results == {1: expected, 2: expected}
    assert [r["level"].name for r in records] == ["ERROR"]
    assert "Oversized body" in records[0]["message"]


# --- token: 498, refusal ----------------------------------------------------


@pytest.mark.asyncio
async def test_498_invalidates_the_pair_and_repeats_the_same_batch_once() -> None:
    """AC2(д). Mutations: no repeat; repeat with the old pair; a different
    ``nm`` in the repeat."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    ok = httpx.Response(200, content=_payload(_product(1, 100), _product(2, 200)))
    results = await _fetch(_recording([httpx.Response(498), ok], seen), [1, 2], access)

    assert results == {
        1: MarketplaceProductData(name="Товар", price=100),
        2: MarketplaceProductData(name="Товар", price=200),
    }
    assert len(seen) == 2
    assert access.get_calls == 2
    assert access.invalidated == [make_pair(1)]
    assert _raw_nm(seen[0]) == _raw_nm(seen[1]) == "1;2"
    assert seen[0].headers["cookie"] == "x_wbaas_token=TOKEN-MARKER-1"
    assert seen[1].headers["cookie"] == "x_wbaas_token=TOKEN-MARKER-2"
    assert seen[1].headers["deviceid"] == make_pair(2).device_id


@pytest.mark.asyncio
async def test_498_twice_is_blocked_for_every_article_after_two_requests() -> None:
    """AC2(д). Mutation: a third request, or ``blocked`` for the first only."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    records, sink_id = _capture()
    try:
        results = await _fetch(
            _recording([httpx.Response(498)] * 3, seen), [1, 2], access
        )
    finally:
        logger.remove(sink_id)

    expected = _failure(FetchFailureReason.blocked, "HTTP 498")
    assert results == {1: expected, 2: expected}
    assert len(seen) == 2
    assert access.get_calls == 2
    assert access.invalidated == [make_pair(1), make_pair(2)]
    # Four lines for the whole chunk (two statuses, the retry note, the verdict),
    # all naming the batch, none naming a single article.
    assert len(records) == 4
    assert all("batch of 2 articles" in r["message"] for r in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", list(WbAccessStage))
async def test_provider_refusal_is_blocked_for_every_article_without_a_request(
    stage: WbAccessStage,
) -> None:
    """AC2(д). Mutation: a request is made anyway, or one article is missed."""
    seen: list[httpx.Request] = []
    access = StubWbAccess(WbAccessUnavailable(stage, "provider detail"))
    records, sink_id = _capture()
    try:
        results = await _fetch(
            _recording(httpx.Response(200, content=_payload()), seen), [1, 2], access
        )
    finally:
        logger.remove(sink_id)

    expected = _failure(FetchFailureReason.blocked, f"WB access unavailable: {stage}")
    assert results == {1: expected, 2: expected}
    assert seen == []
    assert access.get_calls == 1
    assert [r["level"].name for r in records] == ["WARNING"]


@pytest.mark.asyncio
async def test_498_then_empty_products_is_not_found_after_one_retry() -> None:
    """The repeat's answer goes through the batch parser."""
    seen: list[httpx.Request] = []
    results = await _fetch(
        _recording(
            [httpx.Response(498), httpx.Response(200, content=_payload())], seen
        ),
        [1, 2],
    )

    empty = _failure(FetchFailureReason.not_found, "empty products")
    assert results == {1: empty, 2: empty}
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_every_chunk_answered_498_costs_two_requests_and_two_gets() -> None:
    """AC2(е): 250 articles, ``498`` on every answer -> at most ``2 * 3`` requests
    and ``2 * 3`` provider ``get`` calls. Mutation: a retry loop without a cap."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    results = await _fetch(
        _recording(httpx.Response(498), seen), list(range(1, 251)), access
    )

    assert len(seen) == 6
    assert access.get_calls == 6
    assert set(results) == set(range(1, 251))
    assert {
        r.reason for r in results.values() if isinstance(r, MarketplaceFetchFailure)
    } == {FetchFailureReason.blocked}


# --- secrets (AC2 з) -----------------------------------------------------------

_SECRET_SCENARIOS: dict[str, list[httpx.Response]] = {
    "ok": [httpx.Response(200, content=_payload(_product(1), _product(2, None)))],
    "498-recovered": [httpx.Response(498), httpx.Response(200, content=_payload())],
    "498-twice": [httpx.Response(498), httpx.Response(498)],
    "403": [httpx.Response(403)],
    "429": [httpx.Response(429)],
    "500": [httpx.Response(500)],
    "bad-json": [httpx.Response(200, content=b"not json")],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(_SECRET_SCENARIOS))
async def test_token_and_deviceid_never_reach_logs_or_detail_of_a_batch(
    scenario: str,
) -> None:
    """AC2(з). Positive control: the request carried both markers and the sink
    captured records."""
    seen: list[httpx.Request] = []
    records, sink_id = _capture()
    try:
        results = await _fetch(
            _recording(_SECRET_SCENARIOS[scenario], seen), [1, 2], StubWbAccess()
        )
    finally:
        logger.remove(sink_id)

    pair = make_pair(1)
    assert pair.token in seen[0].headers["cookie"]
    assert pair.device_id in seen[0].headers["deviceid"]
    assert records
    haystack = repr(results) + "".join(
        f"{r['message']}{r['extra']}{r['exception']}" for r in records
    )
    assert "TOKEN-MARKER" not in haystack
    assert pair.device_id not in haystack
