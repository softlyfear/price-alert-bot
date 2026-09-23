"""Unit tests for app.schemas.wb.

Facts asserted here are extracted from `tests/fixtures/wb/detail_860043555.json`
by reading the file, not by restating the numbers by hand - that fixture is
the customer-supplied WB response for article `860043555` (PROJECT.md
section 2.8), with eight keys removed at any nesting depth (PROJECT.md
section 8.2, redaction of попытка 2):

- `payload` - an opaque per-size token of unknown purpose.
- `dist` - distance to a warehouse, computed relative to the customer's
  delivery region.
- `time1`, `time2` - delivery time estimates, also region-dependent.
- `wh` - warehouse identifier tied to the region-dependent stock split.
- `priority` - warehouse selection priority, same region dependency.
- `dtype`, `ndtype` - delivery-related fields of unestablished semantics,
  removed on the same "derived from region, unknown otherwise" principle.

`qty` is kept - it backs the fact in PROJECT.md section 2.8, point 5.
Payloads built inline in individual tests (missing keys, unknown extra
fields, a non-JSON body) are synthetic and marked as such; they are not
claims about a WB contract.

No network, no asyncio - the only I/O is one fixture read on the module,
shared by every test through module-scoped fixtures (not at import time,
so a schema regression fails the specific dependent test rather than the
whole module's collection).
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.wb import WbDetailResponse
from app.schemas.wb import WbPrice
from app.schemas.wb import WbProduct
from app.schemas.wb import WbSize

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "wb"
    / "detail_860043555.json"
)

_REMOVED_KEYS = (
    "payload",
    "dist",
    "time1",
    "time2",
    "wh",
    "priority",
    "dtype",
    "ndtype",
)
_PRICED_KOPECKS = 142900
_SIZE_NAMES_WITHOUT_PRICE = frozenset({"36", "40", "42"})


@pytest.fixture(scope="module")
def raw_fixture_bytes() -> bytes:
    """The one disk read shared by every test in this module (AC9)."""
    return _FIXTURE_PATH.read_bytes()


@pytest.fixture(scope="module")
def raw_fixture(raw_fixture_bytes: bytes) -> dict[str, Any]:
    """Parsed fixture as a plain dict, derived from the single disk read."""
    result: dict[str, Any] = json.loads(raw_fixture_bytes)
    return result


@pytest.fixture(scope="module")
def parsed_fixture(raw_fixture: dict[str, Any]) -> WbDetailResponse:
    """Schema-validated fixture, built once per module - a regression here
    fails only the tests that depend on this fixture, not module collection
    (Team Lead's Hint 2, попытка 2)."""
    return WbDetailResponse.model_validate(raw_fixture)


def test_fixture_is_valid_json_object(raw_fixture: dict[str, Any]) -> None:
    """Sanity check mirroring `jq -e .` from the ticket's AC1."""
    assert isinstance(raw_fixture, dict)


def test_fixture_has_no_removed_fields(raw_fixture_bytes: bytes) -> None:
    """None of the eight keys named in the module docstring appear anywhere
    in the fixture (PROJECT.md section 8.2, AC1″)."""
    text = raw_fixture_bytes.decode("utf-8")
    for key in _REMOVED_KEYS:
        assert f'"{key}"' not in text


def test_schema_extracts_product_id_equal_to_requested_article(
    parsed_fixture: WbDetailResponse,
) -> None:
    """`id` equals the requested article (PROJECT.md section 2.8, point 2;
    AC2)."""
    assert parsed_fixture.products[0].id == 860043555


def test_schema_extracts_product_name_verbatim(
    parsed_fixture: WbDetailResponse,
) -> None:
    """`name` matches the fact verbatim, 36 characters (AC2)."""
    name = parsed_fixture.products[0].name
    assert name == "Кроссовки женские на высокой подошве"
    assert len(name) == 36


def test_schema_extracts_eight_sizes(parsed_fixture: WbDetailResponse) -> None:
    """AC2: 8 size entries."""
    assert len(parsed_fixture.products[0].sizes) == 8


def test_schema_extracts_price_for_sizes_with_stock(
    raw_fixture: dict[str, Any], parsed_fixture: WbDetailResponse
) -> None:
    """Five sizes carry a price, all equal to 142900 kopecks in the fact
    (PROJECT.md section 2.8, points 3-4; AC2)."""
    raw_sizes = raw_fixture["products"][0]["sizes"]
    parsed_sizes = parsed_fixture.products[0].sizes
    priced = [
        parsed
        for raw, parsed in zip(raw_sizes, parsed_sizes, strict=True)
        if raw["name"] not in _SIZE_NAMES_WITHOUT_PRICE
    ]
    assert len(priced) == 5
    assert all(size.price is not None for size in priced)
    assert all(size.price.product == _PRICED_KOPECKS for size in priced if size.price)


def test_schema_leaves_price_none_for_sizes_without_stock(
    raw_fixture: dict[str, Any], parsed_fixture: WbDetailResponse
) -> None:
    """Sizes 36, 40, 42 have no `price` key at all upstream (fact,
    PROJECT.md section 2.8, point 3; AC2)."""
    raw_sizes = raw_fixture["products"][0]["sizes"]
    parsed_sizes = parsed_fixture.products[0].sizes
    unpriced = [
        parsed
        for raw, parsed in zip(raw_sizes, parsed_sizes, strict=True)
        if raw["name"] in _SIZE_NAMES_WITHOUT_PRICE
    ]
    assert len(unpriced) == 3
    assert all(size.price is None for size in unpriced)


def test_schema_tolerates_unknown_fields(raw_fixture: dict[str, Any]) -> None:
    """`extra="ignore"` (named in the module docstring) accepts fields the
    project does not consume - the real response has 35 keys on
    `products[0]` alone (AC3). The complementary negative demonstration
    (`extra="forbid"` on a copy rejects the same payload) is a mutation on
    a copy outside the working tree, reported separately - not a pytest
    case, since it asserts about code that is not shipped."""
    payload: dict[str, Any] = copy.deepcopy(raw_fixture)
    payload["unknownTopLevelField"] = "synthetic"
    payload["products"][0]["unknownProductField"] = "synthetic"
    payload["products"][0]["sizes"][0]["unknownSizeField"] = "synthetic"

    result = WbDetailResponse.model_validate(payload)

    assert result.products[0].id == 860043555


def test_price_product_rejects_string() -> None:
    """Strict `int` rejects a numeric string on `WbPrice.product` in Python
    mode (AC4)."""
    with pytest.raises(ValidationError):
        WbPrice.model_validate({"product": "142900"})


def test_price_product_rejects_float() -> None:
    """Strict `int` rejects a float on `WbPrice.product` (AC4)."""
    with pytest.raises(ValidationError):
        WbPrice.model_validate({"product": 1429.0})


def test_product_id_rejects_string() -> None:
    """Strict `int` rejects a numeric string on `WbProduct.id` (AC4)."""
    with pytest.raises(ValidationError):
        WbProduct.model_validate({"id": "860043555", "name": "x", "sizes": []})


def test_price_product_rejects_string_in_json_mode() -> None:
    """Strictness is enforced separately in JSON mode - `model_validate_json`
    is the entry point `WbClient` (PAB-014) actually uses to parse the raw
    response body (pydantic docs, "Strict Mode" and "JSON Parsing"; Team
    Lead's Hint 3, попытка 2)."""
    with pytest.raises(ValidationError):
        WbPrice.model_validate_json('{"product": "142900"}')


def test_schema_parses_fixture_bytes_via_json_mode(
    raw_fixture_bytes: bytes, parsed_fixture: WbDetailResponse
) -> None:
    """PAB-014 feeds the raw HTTP response body (bytes) into
    `model_validate_json` - the JSON-mode parse of the very same bytes
    yields the same facts as the Python-mode parse used by the rest of this
    module (редакция попытки 2, пункт 6б)."""
    result = WbDetailResponse.model_validate_json(raw_fixture_bytes)

    assert result.products[0].id == parsed_fixture.products[0].id
    result_prices = [
        size.price.product if size.price else None for size in result.products[0].sizes
    ]
    expected_prices = [
        size.price.product if size.price else None
        for size in parsed_fixture.products[0].sizes
    ]
    assert result_prices == expected_prices


def test_wb_models_reject_reassignment(raw_fixture: dict[str, Any]) -> None:
    """`frozen=True` on all four models rejects attribute reassignment with
    `frozen_instance` - same shape as
    `test_marketplace_fetch_failure_rejects_reassignment` (AC12″).

    Every instance mutated here is its own, built fresh from `raw_fixture`
    via `model_validate` rather than taken from the module-scoped
    `parsed_fixture` fixture - a failed `setattr` still raises before
    mutating the target, but sharing instances with other tests made the
    isolation depend on test order (PAB-064, Description point 1)."""
    price = WbPrice.model_validate({"product": 100})
    size = WbSize.model_validate({})
    product = WbProduct.model_validate(raw_fixture["products"][0])
    response = WbDetailResponse.model_validate(raw_fixture)

    mutations: tuple[tuple[Any, str, Any], ...] = (
        (price, "product", 200),
        (size, "price", None),
        (product, "name", "x"),
        (response, "products", []),
    )
    for instance, field, value in mutations:
        with pytest.raises(ValidationError) as exc_info:
            setattr(instance, field, value)
        assert exc_info.value.errors()[0]["type"] == "frozen_instance"


def test_qty_sum_does_not_equal_total_quantity(raw_fixture: dict[str, Any]) -> None:
    """Fact backing the module docstring's justification for keeping `qty`
    out of the schema (PROJECT.md section 2.8, point 5; PAB-064 AC2): the
    `qty` of sizes that carry stock, in size order, is `[30, 49, 1, 1, 21]`
    (sum `102`), while `totalQuantity` is `49` - the two disagree. Checked
    on raw fixture data, not through the schema: `stocks`/`qty`/
    `totalQuantity` are deliberately not modeled."""
    raw_product = raw_fixture["products"][0]
    qty_values = [
        stock["qty"] for size in raw_product["sizes"] for stock in size["stocks"]
    ]

    assert qty_values == [30, 49, 1, 1, 21]
    assert sum(qty_values) == 102
    assert raw_product["totalQuantity"] == 49
    assert sum(qty_values) != raw_product["totalQuantity"]


def test_non_json_body_raises_validation_error() -> None:
    """A non-JSON body - the shape of the antibot HTML stub observed for
    this project (PROJECT.md section 2.6) - fed into `model_validate_json`
    raises `pydantic.ValidationError`, the same class used for structural
    validation failures (AC5, Team Lead's Hint 3)."""
    html_stub = "<html>\r\n<title>403 Forbidden</title>"

    with pytest.raises(ValidationError):
        WbDetailResponse.model_validate_json(html_stub)


def test_missing_products_key_raises_validation_error(
    raw_fixture: dict[str, Any],
) -> None:
    """AC5, cell 1 of 3."""
    payload: dict[str, Any] = copy.deepcopy(raw_fixture)
    del payload["products"]

    with pytest.raises(ValidationError):
        WbDetailResponse.model_validate(payload)


def test_missing_name_key_raises_validation_error(raw_fixture: dict[str, Any]) -> None:
    """AC5, cell 2 of 3."""
    payload: dict[str, Any] = copy.deepcopy(raw_fixture)
    del payload["products"][0]["name"]

    with pytest.raises(ValidationError):
        WbDetailResponse.model_validate(payload)


def test_missing_sizes_key_raises_validation_error(raw_fixture: dict[str, Any]) -> None:
    """AC5, cell 3 of 3."""
    payload: dict[str, Any] = copy.deepcopy(raw_fixture)
    del payload["products"][0]["sizes"]

    with pytest.raises(ValidationError):
        WbDetailResponse.model_validate(payload)
