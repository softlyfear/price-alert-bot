"""Selected-size regression against a captured WB response and its derivatives."""

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from app.services.price_service import select_wb_size
from app.services.wb_client import WbClient
from tests.unit.wb_access_stub import StubWbAccess

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/wb/selected_size.json"
_SIZE = 41036292


def _payload() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_bytes())  # type: ignore[no-any-return]


async def _fetch(payload: dict[str, Any], *, batch: bool = False) -> Any:
    article = payload["products"][0]["id"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        client = WbClient(http, StubWbAccess())
        if batch:
            return (await client.get_products_data([article]))[article]
        return await client.get_product_data(article)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_captured_size_projects_its_price_without_changing_legacy(
    batch: bool,
) -> None:
    result = await _fetch(_payload(), batch=batch)
    assert isinstance(result, MarketplaceProductData)
    before = result.model_dump()
    assert result.price == 521900
    assert result.wb_sizes is not None
    assert len(result.wb_sizes) == 41
    assert result.wb_sizes[_SIZE].name == "54-188"
    selected = select_wb_size(result, _SIZE)
    assert isinstance(selected, MarketplaceProductData)
    assert selected.price == 599200
    assert result.model_dump() == before
    legacy = select_wb_size(result, 0)
    assert isinstance(legacy, MarketplaceProductData)
    assert legacy.price == 521900


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("missing", [False, True])
async def test_missing_or_unpriced_selection_is_stock_failure(
    batch: bool, missing: bool
) -> None:
    payload = _payload()
    sizes = payload["products"][0]["sizes"]
    selected = next(s for s in sizes if s["optionId"] == _SIZE)
    if missing:
        sizes.remove(selected)
    else:
        selected.pop("price", None)
    result = await _fetch(payload, batch=batch)
    assert isinstance(result, MarketplaceProductData)
    assert select_wb_size(result, _SIZE) == MarketplaceFetchFailure(
        reason=FetchFailureReason.out_of_stock
    )
    assert result.price == 521900


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_duplicate_option_is_bad_payload(batch: bool) -> None:
    payload = _payload()
    sizes = payload["products"][0]["sizes"]
    sizes.append(copy.deepcopy(sizes[0]))
    result = await _fetch(payload, batch=batch)
    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason == FetchFailureReason.bad_payload


@pytest.mark.parametrize("reason", list(FetchFailureReason))
def test_projection_preserves_upstream_failures(reason: FetchFailureReason) -> None:
    failure = MarketplaceFetchFailure(reason=reason, detail="failure")
    assert select_wb_size(failure, _SIZE) == failure
