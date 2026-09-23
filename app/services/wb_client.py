"""Get product name and price from the Wildberries `u-card/cards/v4/detail` API.

Contract established by fact, not by a live call (PAB-014, "Перепланирование
WB по факту", 2026-09-23): the sole reference is the customer-supplied
capture for article `860043555`, dated 2026-09-23
(`tests/fixtures/wb/detail_860043555.json`), validated here through
`WbDetailResponse` (`app/schemas/wb.py`, PAB-061). Price is in kopecks -
confirmed against the customer's UI (`PROJECT.md` section 2.8, point 4:
`1 429 руб.` corresponds to `price.product = 142900` with a divisor of 100).

Price selection: the tracked price is the **minimum** `price.product` across
sizes that carry a `price` at all - a size with no stock has no `price` key
in the upstream response (fact, section 2.8, point 3), so the first size in
the list is not a reliable source of price (the finding that triggered this
rewrite: `sizes[0]` of the reference article has no price whatsoever). If no
size has a price the product is reported as `out_of_stock`, not
`bad_payload` (`PROJECT.md` section 2.6). Product selection: `products[].id`
is matched against the requested article, never list position (section
2.8) - a positional read would silently attribute a different product's
price to the requested one.

Known limitation, named explicitly per the customer's product decision
(`PROJECT.md` section 2.9): the reported price excludes personal discounts
(WB Wallet, the `spp` "loyal customer" coefficient sent in the request) -
the site may show the user a lower price than the bot.

Access remains blocked from this working environment regardless of the
contract being known: both `www.wildberries.ru/__internal/u-card/cards/v4/detail`
(used below) and the former `card.wb.ru/cards/v4/detail` answer HTTP 403 on
every independent check made for this project, most recently 2026-09-23
(`PROJECT.md` section 2.6). Two request parameters remain named assumptions
rather than verified facts: `dest` is pinned to the value already present in
this module before this rewrite (not the customer's own delivery region,
which is never recorded - `PROJECT.md` section 8.2), and `spp=30` is sent
verbatim as captured, with its exact effect on the returned price
unestablished (section 2.8).
"""

import httpx
from loguru import logger
from pydantic import ValidationError

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.schemas.wb import WbDetailResponse
from app.schemas.wb import WbProduct
from app.schemas.wb import WbSize
from app.services.base_client import BaseMarketplaceClient


def _select_wb_product(products: list[WbProduct], article: int) -> WbProduct | None:
    """Pick the product entry whose `id` equals the requested article.

    List position carries no meaning (`PROJECT.md` section 2.8) - a product
    list that does not contain the requested article at all (including an
    empty list) yields `None`, the caller's cue for `bad_payload`.
    """
    for product in products:
        if product.id == article:
            return product
    return None


def _select_wb_price(sizes: list[WbSize]) -> int | None:
    """Pick the tracked price: the minimum `price.product` across sizes that
    have a price at all (`PROJECT.md` section 2.8). `None` means no size has
    a price, the caller's cue for `out_of_stock`. One pass, `O(S)`.
    """
    prices = [size.price.product for size in sizes if size.price is not None]
    if not prices:
        return None
    return min(prices)


class WbClient(BaseMarketplaceClient):
    URL = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
    TIMEOUT_SECONDS = 10.0

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        """Fetch name and price for a single WB article.

        Returns either `MarketplaceProductData` or a `MarketplaceFetchFailure`
        marking one of `blocked`, `not_found`, `transport_error`,
        `bad_payload` or `out_of_stock` - never `None`, never an exception
        escaping to the caller (`BaseMarketplaceClient` contract).
        """

        params: dict[str, str | int | bool] = {
            "appType": 1,
            "curr": "rub",
            "dest": -1257786,
            "spp": 30,
            "hide_vflags": 4294967296,
            "hide_dflags": 1048576,
            "mtype": 257,
            "lang": "ru",
            "ab_testing": False,
            "nm": str(article),
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        try:
            r = await self._http.get(
                self.URL,
                params=params,
                headers=headers,
                timeout=self.TIMEOUT_SECONDS,
            )

            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.warning(f"Error HTTP {status} for article: {article}")
            if status in (403, 429):
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.blocked,
                    detail=f"HTTP {status}",
                )
            if status == 404:
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.not_found,
                    detail=f"HTTP {status}",
                )
            # Any other status raise_for_status() can produce - 5xx and any
            # unlisted 4xx alike - is treated as a transient transport
            # failure, the catch-all bucket per the ticket's Description.
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.transport_error,
                detail=f"HTTP {status}",
            )
        except httpx.HTTPError as e:
            logger.warning(f"Couldn't get data for article {article}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.transport_error,
                detail=type(e).__name__,
            )

        try:
            parsed = WbDetailResponse.model_validate_json(r.content)

            product = _select_wb_product(parsed.products, article)
            if product is None:
                logger.error(f"No product with id == article {article} in payload")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.bad_payload,
                    detail="no product with matching id",
                )

            price = _select_wb_price(product.sizes)
            if price is None:
                logger.info(f"No size has a price for article {article} - out of stock")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.out_of_stock,
                    detail="no size has a price",
                )

            return MarketplaceProductData(name=product.name, price=price)
        except ValidationError as e:
            # A closed, named exception set: `WbDetailResponse.model_validate_json`
            # and `MarketplaceProductData(...)` are the only two calls in this
            # block that can raise, and both raise exactly this one class - a
            # non-JSON body raises it too, since `pydantic.ValidationError` is
            # a `ValueError` subclass that wraps the JSON decode failure
            # (measured fact, TASKS.md "Перепланирование WB по факту").
            # `str(e)` is never used - it echoes the offending input value.
            logger.error(f"Bad payload for article {article}: {type(e).__name__}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload,
                detail=type(e).__name__,
            )
