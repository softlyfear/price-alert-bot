"""Unit tests for app.services.wb_client.WbClient.

Every test in this file replaces the network with `httpx.MockTransport`
(via `_client_with` below) - no test performs a real network call. The
success path (`test_success_on_full_fixture_returns_marketplace_product_data`)
feeds the real customer-supplied response byte-for-byte
(`tests/fixtures/wb/detail_860043555.json`, `PROJECT.md` section 2.8). Every
other body in this file is a derived or wholly synthetic payload built to
exercise one branch of `WbClient.get_product_data` and is marked as such in
its own docstring - it is not a claim about a WB contract beyond what
`app/schemas/wb.py` (PAB-061) already establishes.

Removed from the попытка 1 version of this file, with reasons (AC11'):

- `test_success_returns_marketplace_product_data` - exercised the retired
  `sizes[0]` selection on a synthetic payload; superseded by the
  full-fixture success test below, which also stands as the AC17'(a)
  regression guard (the reference article's real `sizes[0]` has no price
  at all).
- `test_bad_payload_when_products_list_is_empty` - the outcome (`bad_payload`)
  is unchanged, but the `detail` string it asserted ("empty products list")
  belongs to the retired ad-hoc parsing; superseded by
  `test_bad_payload_when_products_is_empty` below, built on the schema-based
  selection.
- `test_bad_payload_when_sizes_list_is_empty` - the old payload had no `id`
  field, so under `WbDetailResponse` it now fails schema validation for a
  different reason than the one the test named. A product that *is* found
  but whose `sizes` list is empty is no longer `bad_payload` at all - it
  has no priced size, which is `out_of_stock` by the same rule as any other
  size-has-no-price case (`PROJECT.md` section 2.6); see
  `test_out_of_stock_when_no_size_has_a_price`.
- `test_bad_payload_when_price_is_missing` - the old synthetic `price: {}`
  scenario is superseded by the AC13' matrix cells that exercise `WbPrice`
  defects directly (string price, zero price, negative price).
- `test_bad_payload_when_name_is_missing` - superseded by the AC13' matrix
  cells for a non-string name and an over-long name.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import assert_never

import httpx
import pytest
from loguru import logger

if TYPE_CHECKING:
    # ``Message``/``Record`` only exist in loguru's bundled ``.pyi`` stub,
    # not at runtime in ``loguru/__init__.py`` -- importing them
    # unconditionally raises ``ImportError`` (see tests/unit/core/test_database.py).
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

_ARTICLE = 860043555
_PRICE_KOPECKS = 142900
_PRODUCT_NAME = "Кроссовки женские на высокой подошве"
_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "wb"
    / "detail_860043555.json"
)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_SHA256 = "ac6be7b275763c53b8118b4d8ae1b7cf8f57c546f3bc6a607716d540ae944ce0"

_Handler = Callable[[httpx.Request], httpx.Response]


def _client_with(handler: _Handler) -> httpx.AsyncClient:
    """Build an `httpx.AsyncClient` wired to a mock transport only - the
    single place where `AsyncClient(` and `transport=` occur together, so
    every call site in this file is auditable by construction (AC2')."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json_response(status_code: int, payload: dict[str, Any]) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def _body_response(status_code: int, body: bytes) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return handler


def _raising(exc: httpx.HTTPError) -> _Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# --- Type-level guard on the composition of MarketplaceFetchResult -----
#
# Never called at runtime - a purely static construct (Description item 5,
# PAB-014; second half of the Minor 3 decision from PAB-012, the runtime
# half of which lives in tests/unit/schemas/test_marketplace.py). Measured
# on a copy of app/schemas/marketplace.py outside the working tree (see
# the ticket report): with the real, two-member alias, both `isinstance`
# checks below narrow `result` to `Never` by the final line and
# `assert_never` type-checks clean under `mypy --strict`. Replacing the
# alias with `type MarketplaceFetchResult = object` on that copy leaves
# `result` narrowed only to `object` at the final line (mypy cannot narrow
# a non-union type by exclusion), so `assert_never(result)` fails with
# "Argument 1 to "assert_never" has incompatible type "object"; expected
# "Never""" - a named, reproducible mypy error, not merely a hypothetical.
def _exhaustive_reason_dispatch(result: MarketplaceFetchResult) -> str:
    if isinstance(result, MarketplaceProductData):
        return result.name
    if isinstance(result, MarketplaceFetchFailure):
        return result.reason.value
    assert_never(result)


@pytest.fixture(scope="module")
def fixture_bytes() -> bytes:
    """The real customer-supplied response, read once for this module."""
    return _FIXTURE_PATH.read_bytes()


@pytest.mark.asyncio
async def test_success_on_full_fixture_returns_marketplace_product_data(
    fixture_bytes: bytes,
) -> None:
    """AC3': the real fixture, fed byte-for-byte, yields
    `MarketplaceProductData` with the exact name and price from
    `PROJECT.md` section 2.8. `142900` is kopecks - confirmed against the
    customer's UI (`1 429 руб.`), not a synthetic assumption.

    Also AC17'(a): the fixture's `sizes[0]` (size `36`) has no price at
    all - this is precisely the finding that triggered попытка 2 of this
    ticket. A mutation reverting price selection to `sizes[0].price.product`
    on a copy raises `AttributeError` here instead of returning `142900`.
    """
    client = _client_with(_body_response(200, fixture_bytes))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceProductData(name=_PRODUCT_NAME, price=_PRICE_KOPECKS)


@pytest.mark.asyncio
async def test_fixture_bytes_unchanged_after_use(fixture_bytes: bytes) -> None:
    """AC21': this module never mutates or rewrites the fixture file - its
    checksum still matches the one fixed at PAB-061 acceptance
    (`PROJECT.md` "Перепланирование WB по факту")."""
    assert hashlib.sha256(fixture_bytes).hexdigest() == _FIXTURE_SHA256


@pytest.mark.asyncio
async def test_minimum_price_rule_picks_the_lowest_price_not_the_first() -> None:
    """AC17'(b): a synthetic product with three priced sizes whose minimum
    is neither first nor last in list order. A mutation picking "the first
    priced size" would return `30000`, not the minimum `10000`."""
    payload = {
        "products": [
            {
                "id": _ARTICLE,
                "name": "Кроссовки",
                "sizes": [
                    {"price": {"product": 30000}},
                    {"price": {"product": 10000}},
                    {"price": {"product": 20000}},
                ],
            }
        ]
    }
    client = _client_with(_json_response(200, payload))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceProductData(name="Кроссовки", price=10000)


@pytest.mark.asyncio
async def test_product_selected_by_id_not_by_list_position() -> None:
    """AC19': two products, the requested article is second in the list. A
    mutation reading `products[0]` unconditionally would return the first
    product's price (`50000`) instead of the requested one's (`77700`)."""
    payload = {
        "products": [
            {
                "id": 111,
                "name": "Другой товар",
                "sizes": [{"price": {"product": 50000}}],
            },
            {
                "id": _ARTICLE,
                "name": "Нужный товар",
                "sizes": [{"price": {"product": 77700}}],
            },
        ]
    }
    client = _client_with(_json_response(200, payload))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceProductData(name="Нужный товар", price=77700)


@pytest.mark.asyncio
async def test_bad_payload_on_non_json_body() -> None:
    """AC13', cell 'не-JSON': the HTML antibot stub observed for this
    project (`PROJECT.md` section 2.6) is not valid JSON;
    `model_validate_json` raises `pydantic.ValidationError` on it (fact -
    see `tests/unit/schemas/test_wb.py::test_non_json_body_raises_validation_error`),
    caught by the same closed exception set as a structural defect."""
    html_stub = b"<html>\r\n<title>403 Forbidden</title>"
    client = _client_with(_body_response(200, html_stub))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )


@pytest.mark.asyncio
async def test_bad_payload_when_products_is_not_a_list() -> None:
    """AC13', cell 'products не список': synthetic minimal payload."""
    client = _client_with(_json_response(200, {"products": "oops"}))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )


@pytest.mark.asyncio
async def test_bad_payload_when_sizes_is_not_a_list() -> None:
    """AC13', cell 'sizes не список': synthetic minimal payload."""
    payload = {"products": [{"id": _ARTICLE, "name": "Товар", "sizes": "oops"}]}
    client = _client_with(_json_response(200, payload))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )


@pytest.mark.asyncio
async def test_bad_payload_when_name_is_not_a_string() -> None:
    """AC13', cell 'имя не строка'. Also the anchor for AC14': the log
    record and `detail` are inspected for the sentinel value `424242` used
    as the offending name, to prove neither leaks the offending input -
    only the exception's class name is used."""
    payload = {
        "products": [
            {"id": _ARTICLE, "name": 424242, "sizes": [{"price": {"product": 100}}]}
        ]
    }
    client = _client_with(_json_response(200, payload))

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    try:
        async with client:
            wb_client = WbClient(client, StubWbAccess())
            result = await wb_client.get_product_data(_ARTICLE)
    finally:
        logger.remove(sink_id)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )
    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail is not None
    assert "424242" not in result.detail
    assert len(records) == 1
    assert records[0]["level"].name == "ERROR"
    assert "424242" not in records[0]["message"]


@pytest.mark.asyncio
async def test_bad_payload_when_name_exceeds_marketplace_product_data_limit() -> None:
    """AC13', cell 'имя длиннее предела MarketplaceProductData': passes
    `WbProduct.name` (unbounded) but is rejected building
    `MarketplaceProductData` (`max_length=150`, `PROJECT.md` section 10.1
    point 3)."""
    payload = {
        "products": [
            {"id": _ARTICLE, "name": "x" * 151, "sizes": [{"price": {"product": 100}}]}
        ]
    }
    client = _client_with(_json_response(200, payload))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )


@pytest.mark.asyncio
async def test_bad_payload_when_price_product_is_a_string() -> None:
    """AC13', cell 'price.product строкой': `WbPrice.product` is strict
    `int`, rejecting a numeric string even in JSON mode."""
    payload = {
        "products": [
            {"id": _ARTICLE, "name": "Товар", "sizes": [{"price": {"product": "100"}}]}
        ]
    }
    client = _client_with(_json_response(200, payload))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )


@pytest.mark.asyncio
async def test_bad_payload_when_price_product_is_zero() -> None:
    """AC13', cell 'price.product = 0': `WbPrice` accepts `0` (no sign
    check - `PROJECT.md` section 2.8), but `MarketplaceProductData` rejects
    it (`gt=0`, one point of truth for the positivity boundary)."""
    payload = {
        "products": [
            {"id": _ARTICLE, "name": "Товар", "sizes": [{"price": {"product": 0}}]}
        ]
    }
    client = _client_with(_json_response(200, payload))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )


@pytest.mark.asyncio
async def test_bad_payload_when_one_size_price_is_negative_among_normal() -> None:
    """AC13', cell 'price.product < 0 среди нормальных': one size has a
    negative price (`-1`) alongside two ordinarily-priced sizes. The
    minimum is computed across raw `int`s *before* `MarketplaceProductData`
    is built, so `-1` becomes the selected minimum and the whole product -
    not just that one size - is reported `bad_payload`, not a partial
    success with the other two prices (AC13', oговорка PAB-061 приёмки)."""
    payload = {
        "products": [
            {
                "id": _ARTICLE,
                "name": "Товар",
                "sizes": [
                    {"price": {"product": 100}},
                    {"price": {"product": -1}},
                    {"price": {"product": 200}},
                ],
            }
        ]
    }
    client = _client_with(_json_response(200, payload))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="ValidationError"
    )


@pytest.mark.asyncio
async def test_not_found_when_products_is_empty() -> None:
    """PAB-079 AC3(ж), changed assertion: AC13' of PAB-014 had `products == []`
    as `bad_payload` / "no product with matching id". The measured fact
    (`PROJECT.md` section 2.8) is that an unknown article is `200` with an
    empty list, so it is now `not_found` / "empty products"; a non-empty list
    without the requested id stays `bad_payload` (next test)."""
    client = _client_with(_json_response(200, {"products": []}))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.not_found, detail="empty products"
    )


@pytest.mark.asyncio
async def test_bad_payload_when_no_product_matches_requested_article(
    fixture_bytes: bytes,
) -> None:
    """AC13'/AC19' negative counterpart: the real fixture only contains
    `id == 860043555`; requesting a different article finds no match by
    `id`, and list position is never consulted (`PROJECT.md` section 2.8).
    A mutation falling back to `products[0]` on no match would instead
    return the fixture's own product, turning this into a false success."""
    client = _client_with(_body_response(200, fixture_bytes))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(999999999)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="no product with matching id"
    )


@pytest.mark.asyncio
async def test_out_of_stock_when_no_size_has_a_price() -> None:
    """AC13'/AC18': the product is found and structurally valid, but none
    of its sizes carry a `price` - `PROJECT.md` section 2.6's `out_of_stock`,
    logged at `INFO` (section 8.4), not `bad_payload`'s `ERROR`. The log
    level is captured through a real `loguru` sink, not read from the
    source - a mutation swapping the category to `bad_payload` fails the
    `reason` assertion, and a mutation keeping the reason but changing only
    the log call to `.error(...)` fails the level assertion independently."""
    payload = {
        "products": [
            {"id": _ARTICLE, "name": "Товар", "sizes": [{"name": "36"}, {"name": "37"}]}
        ]
    }
    client = _client_with(_json_response(200, payload))

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    try:
        async with client:
            wb_client = WbClient(client, StubWbAccess())
            result = await wb_client.get_product_data(_ARTICLE)
    finally:
        logger.remove(sink_id)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.out_of_stock, detail="no size has a price"
    )
    assert len(records) == 1
    assert records[0]["level"].name == "INFO"


@pytest.mark.asyncio
async def test_blocked_on_http_403() -> None:
    """HTTP 403 maps to `blocked` (Description item 2, AC5 anchor)."""
    client = _client_with(_json_response(403, {}))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="HTTP 403"
    )


@pytest.mark.asyncio
async def test_blocked_on_http_429() -> None:
    """HTTP 429 maps to `blocked`, the same category as 403."""
    client = _client_with(_json_response(429, {}))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="HTTP 429"
    )


@pytest.mark.asyncio
async def test_not_found_on_http_404() -> None:
    """HTTP 404 maps to `not_found` - the HTTP semantics themselves, not a
    claim about WB's payload format (Description item 2)."""
    client = _client_with(_json_response(404, {}))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.not_found, detail="HTTP 404"
    )


@pytest.mark.asyncio
async def test_transport_error_on_http_500() -> None:
    """A 5xx status maps to `transport_error`."""
    client = _client_with(_json_response(500, {}))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail="HTTP 500"
    )


@pytest.mark.asyncio
async def test_transport_error_on_unlisted_4xx_status() -> None:
    """A 4xx status that is neither 403/429 nor 404 falls into the
    `transport_error` catch-all bucket - a deliberate implementation
    decision documented in the ticket report, not a claim about WB."""
    client = _client_with(_json_response(400, {}))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail="HTTP 400"
    )


@pytest.mark.asyncio
async def test_transport_error_on_connect_timeout() -> None:
    """A timeout is a `httpx.TimeoutException`, caught via the
    `httpx.HTTPError` base class (Team Lead's Hint 1) and reported as
    `transport_error`, logged at `WARNING` (AC15')."""
    client = _client_with(_raising(httpx.ConnectTimeout("timed out")))

    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    try:
        async with client:
            wb_client = WbClient(client, StubWbAccess())
            result = await wb_client.get_product_data(_ARTICLE)
    finally:
        logger.remove(sink_id)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.transport_error
    assert result.detail == "ConnectTimeout"
    assert len(records) == 1
    assert records[0]["level"].name == "WARNING"


@pytest.mark.asyncio
async def test_transport_error_on_connection_abort() -> None:
    """A dropped connection is a different leaf of `httpx.HTTPError` than
    a timeout, exercising the same generic branch (Team Lead's Hint 1:
    catch by base class, not by leaf enumeration)."""
    client = _client_with(_raising(httpx.ReadError("connection reset")))
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        result = await wb_client.get_product_data(_ARTICLE)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.transport_error
    assert result.detail == "ReadError"


@pytest.mark.asyncio
async def test_request_uses_new_address_and_exact_parameter_set() -> None:
    """AC20': the request goes to `www.wildberries.ru/__internal/u-card/cards/v4/detail`
    (not the old `card.wb.ru/cards/v4/detail`) with the exact browser
    parameter set, `dest` excepted only in that its value is the one
    already committed to this module before this ticket, not the
    customer's own delivery region (`PROJECT.md` section 2.8) - the exact
    set-equality check below already excludes every other `dest` value
    without naming the customer's region."""
    captured: dict[str, httpx.URL] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"products": []})

    client = _client_with(handler)
    async with client:
        wb_client = WbClient(client, StubWbAccess())
        await wb_client.get_product_data(_ARTICLE)

    url = captured["url"]
    assert url.scheme == "https"
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
        "nm": str(_ARTICLE),
    }
    # Multiset comparison: `dict(url.params)` would collapse a repeated key.
    assert sorted(url.params.multi_items()) == sorted(expected.items())


# --- PAB-063: body size cap, non-2xx bodies unread, overall deadline -----


def _capture_records() -> tuple[list[Record], int]:
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(_sink, level=0)


def _padded_fixture(fixture_bytes: bytes, size: int) -> bytes:
    """The real fixture followed by ASCII spaces up to exactly `size` bytes
    (valid JSON: trailing whitespace is insignificant)."""
    assert len(fixture_bytes) <= size
    return fixture_bytes + b" " * (size - len(fixture_bytes))


def test_max_body_bytes_is_one_mebibyte() -> None:
    """AC4: the threshold named in the ticket."""
    assert WbClient.MAX_BODY_BYTES == 1_048_576


@pytest.mark.asyncio
async def test_body_of_exactly_max_bytes_still_parses(fixture_bytes: bytes) -> None:
    """AC4: the fixture padded to exactly 1 048 576 bytes yields the same
    product as the original. Mutation `>` -> `>=` in the cap fails here."""
    body = _padded_fixture(fixture_bytes, 1_048_576)
    client = _client_with(_body_response(200, body))
    async with client:
        result = await WbClient(client, StubWbAccess()).get_product_data(_ARTICLE)

    assert result == MarketplaceProductData(name=_PRODUCT_NAME, price=_PRICE_KOPECKS)


@pytest.mark.asyncio
async def test_body_one_byte_over_max_is_bad_payload_with_single_error_log(
    fixture_bytes: bytes,
) -> None:
    """AC4: 1 048 577 bytes -> `bad_payload`, exactly one ERROR record, no
    exception escapes."""
    body = _padded_fixture(fixture_bytes, 1_048_577)
    client = _client_with(_body_response(200, body))
    records, sink_id = _capture_records()
    try:
        async with client:
            result = await WbClient(client, StubWbAccess()).get_product_data(_ARTICLE)
    finally:
        logger.remove(sink_id)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="body exceeds 1048576 bytes"
    )
    assert len(records) == 1
    assert records[0]["level"].name == "ERROR"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, FetchFailureReason.blocked),
        (404, FetchFailureReason.not_found),
        (500, FetchFailureReason.transport_error),
    ],
)
@pytest.mark.asyncio
async def test_non_2xx_body_is_never_read(
    status: int, reason: FetchFailureReason
) -> None:
    """AC5: the body generator is never advanced for an error status, and
    the category is unchanged. Reading the body before the status check
    makes the counter non-zero."""
    yielded = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal yielded
        yielded += 1
        yield b"x" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body())

    client = _client_with(handler)
    async with client:
        result = await WbClient(client, StubWbAccess()).get_product_data(_ARTICLE)

    assert yielded == 0
    assert result == MarketplaceFetchFailure(reason=reason, detail=f"HTTP {status}")


@pytest.mark.asyncio
async def test_connection_drop_mid_body_is_transport_error_with_warning() -> None:
    """AC6: one chunk, then `httpx.ReadError` -> `transport_error`,
    `detail == "ReadError"`, one WARNING, nothing raised."""

    async def body() -> AsyncIterator[bytes]:
        yield b'{"products":'
        raise httpx.ReadError("reset")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    client = _client_with(handler)
    records, sink_id = _capture_records()
    try:
        async with client:
            result = await WbClient(client, StubWbAccess()).get_product_data(_ARTICLE)
    finally:
        logger.remove(sink_id)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail="ReadError"
    )
    assert len(records) == 1
    assert records[0]["level"].name == "WARNING"


@pytest.mark.asyncio
async def test_stalled_body_hits_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7: one chunk, then a wait that never ends; with the deadline set to
    0.05 s the call returns `transport_error` / `TimeoutError` with one
    WARNING. The outer 5 s fuse turns a missing deadline into a failure
    (its `TimeoutError` escapes this test) instead of a hang."""
    monkeypatch.setattr(WbClient, "TIMEOUT_SECONDS", 0.05)
    never = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b'{"products":'
        await never.wait()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    client = _client_with(handler)
    records, sink_id = _capture_records()
    try:
        async with client:
            result = await asyncio.wait_for(
                WbClient(client, StubWbAccess()).get_product_data(_ARTICLE), timeout=5
            )
    finally:
        logger.remove(sink_id)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail="TimeoutError"
    )
    assert len(records) == 1
    assert records[0]["level"].name == "WARNING"


# --- PAB-079: access by the anti-bot token --------------------------------

_RECOVERED = MarketplaceProductData(name=_PRODUCT_NAME, price=_PRICE_KOPECKS)


def _sequence(responses: list[httpx.Response], seen: list[httpx.Request]) -> _Handler:
    """Answer the n-th request with `responses[n]`, record every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[len(seen) - 1]

    return handler


def _ok(fixture_bytes: bytes) -> httpx.Response:
    return httpx.Response(200, content=fixture_bytes)


@pytest.mark.asyncio
async def test_request_carries_cookie_and_deviceid_of_the_pair_and_jar_stays_empty(
    fixture_bytes: bytes,
) -> None:
    """AC3(a): the pair travels as headers; the shared jar (used by Ozon too)
    holds nothing afterwards. Expected values are literals of the stub's
    first pair."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    client = _client_with(_sequence([_ok(fixture_bytes)], seen))
    async with client:
        result = await WbClient(client, access).get_product_data(_ARTICLE)
        jar_size = len(client.cookies.jar)

    assert result == _RECOVERED
    assert len(seen) == 1
    assert seen[0].headers["cookie"] == "x_wbaas_token=TOKEN-MARKER-1"
    assert seen[0].headers["deviceid"] == "site_" + "0" * 31 + "1"
    assert jar_size == 0
    assert access.invalidated == []


@pytest.mark.asyncio
async def test_498_invalidates_the_same_pair_then_retries_once_with_a_new_one(
    fixture_bytes: bytes,
) -> None:
    """AC3(b): `498` -> `invalidate(pair 1)` -> `get` -> one repeat carrying
    pair 2 -> the price. Exactly 2 requests and 2 `get` calls."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    client = _client_with(_sequence([httpx.Response(498), _ok(fixture_bytes)], seen))
    async with client:
        result = await WbClient(client, access).get_product_data(_ARTICLE)

    assert result == _RECOVERED
    assert len(seen) == 2
    assert access.get_calls == 2
    assert access.invalidated == [make_pair(1)]
    assert seen[0].headers["cookie"] == "x_wbaas_token=TOKEN-MARKER-1"
    assert seen[1].headers["cookie"] == "x_wbaas_token=TOKEN-MARKER-2"
    assert seen[1].headers["deviceid"] == make_pair(2).device_id


@pytest.mark.asyncio
async def test_498_twice_is_blocked_after_exactly_two_requests() -> None:
    """AC3(в): the second `498` is `blocked` / "HTTP 498"; no third request,
    no third `get`; both pairs were invalidated."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    client = _client_with(
        _sequence([httpx.Response(498), httpx.Response(498), httpx.Response(498)], seen)
    )
    async with client:
        result = await WbClient(client, access).get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="HTTP 498"
    )
    assert len(seen) == 2
    assert access.get_calls == 2
    assert access.invalidated == [make_pair(1), make_pair(2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", list(WbAccessStage))
async def test_provider_refusal_is_blocked_naming_the_stage_without_any_request(
    stage: WbAccessStage,
) -> None:
    """AC3(г): for every stage the result is `blocked` with the stage in
    `detail` and WB is not contacted; the refusal is not retried."""
    seen: list[httpx.Request] = []
    access = StubWbAccess(WbAccessUnavailable(stage, "provider detail"))
    client = _client_with(_sequence([], seen))
    async with client:
        result = await WbClient(client, access).get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail=f"WB access unavailable: {stage}"
    )
    assert seen == []
    assert access.get_calls == 1


@pytest.mark.asyncio
async def test_403_invalidates_the_pair_and_is_blocked_without_a_repeat() -> None:
    """AC3(д): a pair that draws `403` is dropped; one request, one `get`."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    client = _client_with(_sequence([httpx.Response(403), httpx.Response(200)], seen))
    async with client:
        result = await WbClient(client, access).get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="HTTP 403"
    )
    assert len(seen) == 1
    assert access.get_calls == 1
    assert access.invalidated == [make_pair(1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 404, 500, 400])
async def test_other_statuses_do_not_invalidate_the_pair(status: int) -> None:
    """AC3(е): `429` is `blocked` and keeps the pair (the pair is fine, the
    rate is not); the other error statuses keep it too."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    client = _client_with(_sequence([httpx.Response(status)], seen))
    async with client:
        result = await WbClient(client, access).get_product_data(_ARTICLE)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail == f"HTTP {status}"
    assert len(seen) == 1
    assert access.invalidated == []


@pytest.mark.asyncio
async def test_429_is_blocked() -> None:
    """AC3(е): the category of `429` next to the no-`invalidate` check above."""
    client = _client_with(_sequence([httpx.Response(429)], []))
    async with client:
        result = await WbClient(client, StubWbAccess()).get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.blocked, detail="HTTP 429"
    )


@pytest.mark.asyncio
async def test_498_then_empty_products_is_not_found_after_one_retry() -> None:
    """The repeat's result goes through the same parsing: `200` with
    `products == []` after a recovered `498` is `not_found`."""
    seen: list[httpx.Request] = []
    client = _client_with(
        _sequence(
            [httpx.Response(498), httpx.Response(200, json={"products": []})], seen
        )
    )
    async with client:
        result = await WbClient(client, StubWbAccess()).get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.not_found, detail="empty products"
    )
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_498_then_transport_error_on_repeat_is_transport_error() -> None:
    """A transport failure of the repeat is reported as such, not as `blocked`."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(498)
        raise httpx.ConnectTimeout("timed out")

    client = _client_with(handler)
    async with client:
        result = await WbClient(client, StubWbAccess()).get_product_data(_ARTICLE)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail="ConnectTimeout"
    )
    assert len(seen) == 2


_SECRET_SCENARIOS: dict[str, list[httpx.Response]] = {
    "498-recovered": [httpx.Response(498), httpx.Response(200, json={"products": []})],
    "498-twice": [httpx.Response(498), httpx.Response(498)],
    "403": [httpx.Response(403)],
    "429": [httpx.Response(429)],
    "500": [httpx.Response(500)],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(_SECRET_SCENARIOS))
async def test_token_and_deviceid_never_reach_logs_or_detail(scenario: str) -> None:
    """AC6: markers (the pair's token and `deviceid`) are absent from every
    log record of every level and from `detail`. Positive control: the
    request did carry both markers and the sink did capture records."""
    seen: list[httpx.Request] = []
    access = StubWbAccess()
    client = _client_with(_sequence(_SECRET_SCENARIOS[scenario], seen))
    records, sink_id = _capture_records()
    try:
        async with client:
            result = await WbClient(client, access).get_product_data(_ARTICLE)
    finally:
        logger.remove(sink_id)

    pair = make_pair(1)
    assert pair.token in seen[0].headers["cookie"]
    assert pair.device_id in seen[0].headers["deviceid"]
    assert records
    haystack = repr(result) + "".join(
        f"{r['message']}{r['extra']}{r['exception']}" for r in records
    )
    assert pair.token not in haystack
    assert pair.device_id not in haystack
    assert "TOKEN-MARKER" not in haystack


@pytest.mark.asyncio
async def test_refusal_path_logs_the_stage_without_secrets() -> None:
    """AC6 for the provider-refusal path: one `WARNING`, the stage is named."""
    access = StubWbAccess(WbAccessUnavailable(WbAccessStage.paused, "pause"))
    client = _client_with(_sequence([], []))
    records, sink_id = _capture_records()
    try:
        async with client:
            await WbClient(client, access).get_product_data(_ARTICLE)
    finally:
        logger.remove(sink_id)

    assert [r["level"].name for r in records] == ["WARNING"]
    assert "paused" in records[0]["message"]


_LEAK_PROBE = """
import os

import httpx
import pytest

from app.services.wb_client import WbClient
from app.services.wb_access import WbAccess
from tests.unit.wb_access_stub import StubWbAccess


class TransportBoom(Exception):
    pass


class _Env(StubWbAccess):
    async def get(self):
        return WbAccess(os.environ["PROBE_TOKEN"], os.environ["PROBE_DEVICE"])


def _boom(request):
    raise TransportBoom("unhandled transport failure")


@pytest.mark.asyncio
async def test_probe():
    {leak}
    async with httpx.AsyncClient(transport=httpx.MockTransport(_boom)) as http:
        await WbClient(http, _Env()).get_product_data(1)
"""


@pytest.mark.parametrize(
    ("flags", "leak"),
    [
        pytest.param(["-q"], False, id="quiet"),
        pytest.param(["--showlocals", "--tb=long"], False, id="showlocals_long"),
        pytest.param(
            ["--showlocals", "--tb=long"], True, id="control_plain_local_is_printed"
        ),
    ],
)
def test_unhandled_transport_exception_does_not_print_the_pair(
    tmp_path: Path, flags: list[str], leak: bool
) -> None:
    """AC6, subprocess: an exception that is not handled by `WbClient` (a
    non-`httpx` error from the transport) unwinds through its frames. Under
    `--showlocals --tb=long` pytest prints every local of every frame with
    `repr`; the pair's markers must not be among them. The control cell keeps
    the token in a plain local of the probe's own frame and must see it
    printed - proof that the check can fail."""
    token = f"tok-{uuid.uuid4().hex}"
    device = f"site_{uuid.uuid4().hex}"
    probe = tmp_path / "test_probe_pair_leak.py"
    leak_line = "plain = os.environ['PROBE_TOKEN']" if leak else "pass"
    probe.write_text(_LEAK_PROBE.replace("{leak}", leak_line))

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-p", "no:cacheprovider", *flags],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "PROBE_TOKEN": token, "PROBE_DEVICE": device},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "TransportBoom" in output
    assert device not in output
    if leak:
        assert token in output
    else:
        assert token not in output
