"""Schema for the Wildberries `u-card/cards/v4/detail` response.

Contract established by a fact, not a live call: the sole reference response
is the customer-supplied capture for article `860043555`, dated 2026-09-23
(`PROJECT.md` section 2.8). The response is not reproducible from the current
working environment (both known addresses answer `403`).

Only the fields `WbClient` actually reads are modeled; everything else is
deliberately left out and listed here with the reason, rather than silently
dropped:

- `WbPrice.basic` - the crossed-out pre-discount price, useless for a
  "notify when cheaper" threshold.
- `WbPrice.wallet` and the remaining price sub-fields (`logistics`,
  `return`, `cashback`) - not the tracked price; `wallet` is `0` in the
  fact. A third, lower price shown on the customer's page is absent from
  this response entirely; its origin is not verified (`PROJECT.md`
  section 2.8).
- `WbSize.stocks` - stock availability is inferred from the mere presence
  of `price` on a size (a size with no stock has no `price` key at all,
  per the fact), not from parsing `stocks`.
- `WbProduct.totalQuantity` - measured not to equal the sum of
  `sizes[].stocks[].qty` (`PROJECT.md` section 2.8, point 5); unused.
- `WbSize.payload` - an opaque per-size token of unknown purpose, absent
  from the test fixture by project rule (`PROJECT.md` section 8.2).

Price selection (minimum `price.product` across sizes that have a price)
and product selection (by `id`, not by list position) are policy decisions
that belong to the client (PAB-014), not to the shape of the data - this
module only describes the shape.

All models use `extra="ignore"`: the real response carries dozens of fields
(35 keys on `products[0]` alone) that this project does not consume, and a
new field appearing upstream must not turn a sellable product into
`bad_payload`. They are also `frozen=True`: an instance represents a fact
observed at parse time, not evolving state.
"""

from typing import Annotated

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class WbPrice(BaseModel):
    """The price block of a single size.

    `product` is the current price shown on the product page, before
    personal discounts, in kopecks (integer) - fact, confirmed against the
    customer's UI (`PROJECT.md` section 2.8, point 4: `1 429 руб.`
    corresponds to `142900` with a divisor of 100; see section 2.9 for the
    "no personal discounts" caveat).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    product: Annotated[int, Field(strict=True)]


class WbSize(BaseModel):
    """A single size entry of a product.

    `price` is absent from the upstream response entirely for a size with
    no stock (fact, `PROJECT.md` section 2.8, point 3) - modeled as an
    optional field defaulting to `None`, not as a required field with a
    sentinel.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    price: WbPrice | None = None


class WbProduct(BaseModel):
    """A single product entry of the `products` list.

    `id` equals the requested article (fact, `PROJECT.md` section 2.8,
    point 2) and is what the client matches against the requested article,
    rather than trusting list position. `name` carries no length bound here
    - that constraint lives on `MarketplaceProductData` and duplicating it
    here would create a second point of truth.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(strict=True)]
    name: str
    sizes: list[WbSize]


class WbDetailResponse(BaseModel):
    """Top-level shape of the `u-card/cards/v4/detail` response.

    The top level is exactly `["products"]`, with no `data` wrapper (fact,
    `PROJECT.md` section 2.8, point 1). An empty `products` list is valid at
    the schema level; what it means for the client is a policy decision, not
    a structural one.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    products: list[WbProduct]
