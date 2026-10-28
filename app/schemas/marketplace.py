"""Marketplace-agnostic schemas: product data and a marked fetch result."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

# Upper bound on `price` (kopecks): the `current_price`/`previous_price`
# (`products`) and `target_price` (`alerts`) columns are `sa.Integer()` (int4,
# migration `c55fadc558a3`), whose maximum value is 2_147_483_647. A price
# above this limit must fail validation here - the single point of truth for
# "a valid price" - rather than reach the database and fail on the driver.
_MAX_PRICE_KOPECKS = 2_147_483_647


class MarketplaceProductData(BaseModel):
    """Base marketplace validation schema. `price` is in kopecks."""

    name: Annotated[str, Field(min_length=2, max_length=150)]
    price: Annotated[int, Field(gt=0, le=_MAX_PRICE_KOPECKS)]

    model_config = {
        "extra": "forbid",
    }


class FetchFailureReason(StrEnum):
    """Category of a marketplace fetch failure.

    Fixed set of five members per PROJECT.md section 2.6 - membership is not
    meant to grow casually, each value drives distinct user-facing text and
    a distinct log level. `StrEnum` (not `str, Enum`) is chosen deliberately:
    `str(member)` yields the bare value (e.g. "blocked"), which is exactly
    what is wanted both in the `reason` field of a structured log record and
    in comparisons inside `PriceService` - `str, Enum` would instead render
    "FetchFailureReason.blocked".
    """

    not_found = "not_found"
    blocked = "blocked"
    transport_error = "transport_error"
    bad_payload = "bad_payload"
    out_of_stock = "out_of_stock"


class MarketplaceFetchFailure(BaseModel):
    """Marked result of a failed attempt to fetch a marketplace product.

    Represents a fact observed at call time (what kind of failure happened),
    not evolving state, so the model is frozen - reassigning `reason` after
    the response has been parsed would not be meaningful.

    Deliberately excludes: the HTTP status code as a required field (`bad_payload`
    happens on a 200, `transport_error` may have no status at all), the raw
    marketplace response body (unbounded third-party data that would leak into
    logs), and the exception object (not serializable, drags a traceback).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: FetchFailureReason
    detail: Annotated[str | None, Field(max_length=500)] = None


type MarketplaceFetchResult = MarketplaceProductData | MarketplaceFetchFailure
