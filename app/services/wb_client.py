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
(for example WB Wallet) - the site may show the user a lower price than the
bot.

The response body is read as a stream with a size cap (`MAX_BODY_BYTES`) and
the whole request is bounded by one deadline (`TIMEOUT_SECONDS`), see
`PROJECT.md` section 8.2.

Access is by the anti-bot token (`PROJECT.md` section 2.12): every request
carries the pair from a `WbAccessProvider` (`app/services/wb_access.py`). `498`
means the token is spent - the pair is invalidated and the request repeated
once with a fresh one; a second `498` is `blocked`. `403` with a pair also
invalidates it and is `blocked` without a repeat; a provider refusal is
`blocked` without any request to WB.

Direct access (no token) remains blocked from this working environment:
both `www.wildberries.ru/__internal/u-card/cards/v4/detail`
(used below) and the former `card.wb.ru/cards/v4/detail` answer HTTP 403 on
every independent check made for this project, most recently 2026-09-23
(`PROJECT.md` section 2.6). Two request parameters remain named assumptions
rather than verified facts: `dest` is pinned to the value already present in
this module before this rewrite (not the customer's own delivery region,
which is never recorded - `PROJECT.md` section 8.2), and `spp=30` is sent
verbatim as captured, with its exact effect on the returned price
unestablished (section 2.8).
"""

import asyncio
from collections.abc import Sequence
from itertools import batched
from typing import ClassVar
from urllib.parse import urlencode

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
from app.services.base_client import read_body_bounded
from app.services.wb_access import WbAccess
from app.services.wb_access import WbAccessProvider
from app.services.wb_access import WbAccessUnavailable


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


class _TokenRejected:
    """Marker: WB answered 498 - the pair is spent (not a fetch failure yet)."""


class WbClient(BaseMarketplaceClient):
    URL = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
    TIMEOUT_SECONDS = 10.0
    MAX_BODY_BYTES = 1_048_576
    # Batch path only (PAB-081 AC2(i)): 100 articles weigh ~10.5 KB each, so the
    # single-request cap would drop a whole batch on one heavy product.
    MAX_BATCH_BODY_BYTES = 4_194_304
    # Measured weight (PROJECT.md 2.12, R13): 100 articles cost the token one
    # request and ~347 KB. A constant, not a setting.
    batch_size: ClassVar[int] = 100

    def __init__(
        self, http_client: httpx.AsyncClient, access: WbAccessProvider
    ) -> None:
        self._http = http_client
        self._access = access

    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        """Fetch name and price for a single WB article.

        Returns either `MarketplaceProductData` or a `MarketplaceFetchFailure`
        marking one of `blocked`, `not_found`, `transport_error`,
        `bad_payload` or `out_of_stock` - never `None`, never an exception
        escaping to the caller (`BaseMarketplaceClient` contract). At most two
        HTTP requests and two provider `get` calls per article.
        """

        body = await self._fetch(
            str(article), f"article {article}", self.MAX_BODY_BYTES
        )
        if isinstance(body, MarketplaceFetchFailure):
            return body
        return self._parse(article, body)

    async def get_products_data(
        self, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        """Fetch name and price for many WB articles, one request per batch.

        Distinct articles are cut into chunks of `batch_size`; each chunk is a
        single `nm=<a>;<b>;...` request. Products are matched to articles by
        `id`, never by position. A request-level failure marks every article
        of the chunk alike. Every distinct input article is a key; nothing
        escapes. At most two HTTP requests and two provider `get` calls per
        chunk, so `2 * ceil(N / batch_size)` for `N` distinct articles.
        """
        results: dict[int, MarketplaceFetchResult] = {}
        for chunk in batched(dict.fromkeys(articles), self.batch_size, strict=False):
            subject = f"batch of {len(chunk)} articles"
            body = await self._fetch(
                ";".join(map(str, chunk)), subject, self.MAX_BATCH_BODY_BYTES
            )
            if isinstance(body, MarketplaceFetchFailure):
                results.update(dict.fromkeys(chunk, body))
            else:
                results.update(self._parse_batch(chunk, body))
        return results

    async def _fetch(
        self, nm: str, subject: str, max_body_bytes: int
    ) -> bytes | MarketplaceFetchFailure:
        """Request `nm` with a provider pair; on `498` invalidate and repeat once.

        `subject` only labels log lines ("article N" or "batch of N articles").
        """
        attempt = 0
        while True:
            attempt += 1
            access = await self._access.get()
            if isinstance(access, WbAccessUnavailable):
                logger.warning(f"WB access unavailable for {subject}: {access.stage}")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.blocked,
                    detail=f"WB access unavailable: {access.stage}",
                )
            body = await self._request(nm, subject, access, max_body_bytes)
            if not isinstance(body, _TokenRejected):
                return body
            await self._access.invalidate(access)
            if attempt == 2:
                logger.warning(f"Token rejected twice (HTTP 498) for {subject}")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.blocked,
                    detail="HTTP 498",
                )
            logger.info(f"Token rejected (HTTP 498) for {subject}, retrying")

    async def _request(
        self, nm: str, subject: str, access: WbAccess, max_body_bytes: int
    ) -> bytes | MarketplaceFetchFailure | _TokenRejected:
        """One HTTP request with `access`; a failure is returned, not raised."""

        # Built by hand: `httpx` percent-encodes the `;` batch separator in
        # `params`, while WB is measured with the literal one (R13).
        query = urlencode(
            {
                "appType": 1,
                "curr": "rub",
                "dest": -1257786,
                "spp": 30,
                "hide_vflags": 4294967296,
                "hide_dflags": 1048576,
                "mtype": 257,
                "lang": "ru",
                "ab_testing": "false",
                "nm": nm,
            },
            safe=";",
        )

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Accept-Language": "ru-RU,ru;q=0.9",
            **access.request_headers(),
        }

        try:
            # One deadline over connect, headers and body read: httpx timeouts
            # bound a single operation, not the whole request.
            async with (
                asyncio.timeout(self.TIMEOUT_SECONDS),
                self._http.stream(
                    "GET",
                    f"{self.URL}?{query}",
                    headers=headers,
                    timeout=self.TIMEOUT_SECONDS,
                ) as r,
            ):
                # Status is known from headers; a non-2xx body is never read.
                r.raise_for_status()
                body = await read_body_bounded(r, max_body_bytes)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.warning(f"Error HTTP {status} for {subject}")
            if status == 498:
                return _TokenRejected()
            if status == 403:
                # A pair that draws 403 is spoiled: drop it, no repeat.
                await self._access.invalidate(access)
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.blocked,
                    detail=f"HTTP {status}",
                )
            if status == 429:
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
            logger.warning(f"Couldn't get data for {subject}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.transport_error,
                detail=type(e).__name__,
            )
        except TimeoutError as e:
            logger.warning(f"Deadline exceeded for {subject}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.transport_error,
                detail=type(e).__name__,
            )

        if isinstance(body, MarketplaceFetchFailure):
            logger.error(f"Oversized body for {subject}: {body.detail}")
            return body
        return body

    def _parse(self, article: int, body: bytes) -> MarketplaceFetchResult:
        """Validate the payload and pick the product and its price."""
        try:
            parsed = WbDetailResponse.model_validate_json(body)

            if not parsed.products:
                # Fact (`PROJECT.md` section 2.8): an unknown article is
                # `200` with `products == []`.
                logger.warning(f"Empty products for article {article} - not found")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.not_found,
                    detail="empty products",
                )
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

    def _parse_batch(
        self, chunk: Sequence[int], body: bytes
    ) -> dict[int, MarketplaceFetchResult]:
        """Validate a batch payload and map each requested article to a result.

        Matching is by `products[].id`. An empty list is `not_found` for all;
        an article absent from a non-empty list is `not_found` too (assumption
        R13, not a measurement - a wrong mark is harmless, a failure never
        overwrites a stored price). One pass over the products, `O(n)`.
        """
        try:
            parsed = WbDetailResponse.model_validate_json(body)
        except ValidationError as e:
            # `str(e)` is never used - it echoes the offending input value.
            logger.error(
                f"Bad payload for batch of {len(chunk)} articles: {type(e).__name__}"
            )
            failure = MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload,
                detail=type(e).__name__,
            )
            return dict.fromkeys(chunk, failure)

        if not parsed.products:
            logger.warning(f"Empty products for batch of {len(chunk)} articles")
            empty = MarketplaceFetchFailure(
                reason=FetchFailureReason.not_found,
                detail="empty products",
            )
            return dict.fromkeys(chunk, empty)

        by_id: dict[int, WbProduct] = {}
        for item in parsed.products:
            by_id.setdefault(item.id, item)

        results: dict[int, MarketplaceFetchResult] = {}
        absent = 0
        no_price = 0
        for article in chunk:
            product = by_id.get(article)
            if product is None:
                absent += 1
                results[article] = MarketplaceFetchFailure(
                    reason=FetchFailureReason.not_found,
                    detail="absent from batch response",
                )
                continue
            price = _select_wb_price(product.sizes)
            if price is None:
                no_price += 1
                results[article] = MarketplaceFetchFailure(
                    reason=FetchFailureReason.out_of_stock,
                    detail="no size has a price",
                )
                continue
            try:
                results[article] = MarketplaceProductData(
                    name=product.name, price=price
                )
            except ValidationError as e:
                logger.error(f"Bad product data in batch: {type(e).__name__}")
                results[article] = MarketplaceFetchFailure(
                    reason=FetchFailureReason.bad_payload,
                    detail=type(e).__name__,
                )
        if absent or no_price:
            logger.info(
                f"Batch of {len(chunk)} articles: {absent} absent, "
                f"{no_price} out of stock"
            )
        return results
