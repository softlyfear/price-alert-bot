"""WB prices are kopecks; track the minimum priced size without personal discounts.

The response contract comes from tests/fixtures/wb/detail_860043555.json.
`dest` is fixed and the effect of captured `spp=30` is unverified.
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
    for product in products:
        if product.id == article:
            return product
    return None


def _select_wb_price(sizes: list[WbSize]) -> int | None:
    """Unpriced sizes may be out of stock; return the minimum priced size."""
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
    # A single-request cap could drop a batch containing a heavy product.
    MAX_BATCH_BODY_BYTES = 4_194_304
    batch_size: ClassVar[int] = 100

    def __init__(
        self, http_client: httpx.AsyncClient, access: WbAccessProvider
    ) -> None:
        self._http = http_client
        self._access = access

    async def get_product_data(self, article: int) -> MarketplaceFetchResult:

        body = await self._fetch(
            str(article), f"article {article}", self.MAX_BODY_BYTES
        )
        if isinstance(body, MarketplaceFetchFailure):
            return body
        return self._parse(article, body)

    async def get_products_data(
        self, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        """Match by ID; a request failure applies to every article in its batch."""
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
        """Renew a rejected token once; a second 498 is blocked."""
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

        # Built by hand: `httpx` percent-encodes the `;` batch separator in
        # `params`, while WB was observed with the literal one.
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
        try:
            parsed = WbDetailResponse.model_validate_json(body)

            if not parsed.products:
                # WB returns HTTP 200 with an empty list for an unknown article.
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
            # Validation errors may contain the upstream input.
            logger.error(f"Bad payload for article {article}: {type(e).__name__}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload,
                detail=type(e).__name__,
            )

    def _parse_batch(
        self, chunk: Sequence[int], body: bytes
    ) -> dict[int, MarketplaceFetchResult]:
        """An absent article in a non-empty batch is assumed not found."""
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
