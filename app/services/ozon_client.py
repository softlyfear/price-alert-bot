"""Get product name and price from the Ozon `entrypoint-api`.

Contract established by fact, not by a live call (`PROJECT.md` section 2.11):
the sole reference is the customer-supplied capture for article `3593896354`
(`tests/fixtures/ozon/entrypoint_3593896354.json`, a minimal slice), validated
through `app/schemas/ozon.py`. The response is double-encoded: `widgetStates`
maps `<component>-<id>-<container>-<page>` to a *string* of JSON, parsed a
second time per widget.

Policy decisions that live here, not in the schema:

- Widget selection is by the key segment before the first `-` (`str.partition`,
  never `startswith`: `webPrice` and `webPriceDecreasedCompact` are different
  components) and requires exactly one entry. Zero or several is
  `bad_payload`; "first match" would depend on key order.
- Product identity: `webDetailSKU.copyText` must equal the requested article,
  otherwise the price could belong to another SKU (`bad_payload`).
- `isAvailable: false` was never observed, so it is `bad_payload`, not
  `out_of_stock` (follow-up: PAB-075). `not_found` only for HTTP 404.
- The tracked price is `webPrice.price`, without the Ozon card (`PROJECT.md`
  section 2.9): the site may show the user a lower price than the bot.

Anti-bot is an expected mode, reported as `blocked`, never as `not_found`:
HTTP 403/429 and an exhausted redirect chain (without cookies the anti-bot
answers with an endless `307 ... &__rr=N`). No bypass is attempted (customer
decision, `PROJECT.md` section 2.11). The body of a non-2xx response is never
read. The body is read with a size cap and the whole request is bounded by one
deadline, as in `WbClient` (`PROJECT.md` section 8.2).

Redirects are followed by hand (decision R4', section 2.11): `httpx` with
`follow_redirects=True` reads every `3xx` body unbounded and follows to any
host or scheme. Each step here runs with `follow_redirects=False`, a redirect
response is closed unread, the next step is its `next_request` (so cookies of
the shared jar travel as usual), at most `MAX_REDIRECTS` hops are made, and a
hop leaves the origin of `OzonClient.URL` only as `bad_payload` without any
request to the foreign address.
"""

import asyncio
from enum import Enum

import httpx
from loguru import logger
from pydantic import ValidationError

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.schemas.marketplace import MarketplaceProductData
from app.schemas.ozon import OzonDetailSkuState
from app.schemas.ozon import OzonEntrypointResponse
from app.schemas.ozon import OzonPriceState
from app.schemas.ozon import OzonProductHeadingState
from app.services.base_client import BaseMarketplaceClient
from app.services.base_client import read_body_bounded

_HEADING = "webProductHeading"
_PRICE = "webPrice"
_SKU = "webDetailSKU"


class _WidgetProblem(Enum):
    missing = "missing"
    ambiguous = "ambiguous"


def _select_widget_state(
    widget_states: dict[str, str], component: str
) -> str | _WidgetProblem:
    """Return the state string of the single widget of `component`.

    The component is the key segment before the first `-`. Zero entries is
    `_WidgetProblem.missing`, more than one `_WidgetProblem.ambiguous`: the
    caller's cue for `bad_payload`. One pass, `O(W)`.
    """
    found: str | None = None
    for key, state in widget_states.items():
        if key.partition("-")[0] != component:
            continue
        if found is not None:
            return _WidgetProblem.ambiguous
        found = state
    return _WidgetProblem.missing if found is None else found


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Return (scheme, host, port) of `url`; `httpx` drops a default port."""
    return url.scheme, url.host, url.port


class OzonClient(BaseMarketplaceClient):
    URL = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
    TIMEOUT_SECONDS = 10.0
    MAX_BODY_BYTES = 4_194_304
    MAX_REDIRECTS = 5

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def _fetch_body(
        self, article: int, params: dict[str, str], headers: dict[str, str]
    ) -> bytes | MarketplaceFetchFailure:
        """Run the manual redirect loop and read the final 2xx body, bounded.

        Raises `httpx.HTTPStatusError` for a non-2xx final status (including a
        `3xx` without `Location`); the caller maps it. A redirect response is
        closed without reading its body. Time `O(R + B)`, `R <= MAX_REDIRECTS`.
        """
        home = _origin(httpx.URL(self.URL))
        request = self._http.build_request(
            "GET",
            self.URL,
            params=params,
            headers=headers,
            timeout=self.TIMEOUT_SECONDS,
        )
        redirects = 0
        while True:
            response = await self._http.send(
                request, stream=True, follow_redirects=False
            )
            try:
                next_request = response.next_request
                if next_request is None:
                    # Status is known from headers; a non-2xx body is never read.
                    response.raise_for_status()
                    body = await read_body_bounded(response, self.MAX_BODY_BYTES)
                    if isinstance(body, MarketplaceFetchFailure):
                        logger.error(
                            f"Oversized body for Ozon article {article}: {body.detail}"
                        )
                    return body
            finally:
                await response.aclose()
            if _origin(next_request.url) != home:
                logger.error(f"Ozon article {article}: redirect left the Ozon origin")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.bad_payload,
                    detail="redirect left the Ozon origin",
                )
            if redirects >= self.MAX_REDIRECTS:
                logger.warning(f"Ozon redirect limit (anti-bot) for article {article}")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.blocked,
                    detail=f"more than {self.MAX_REDIRECTS} redirects",
                )
            redirects += 1
            request = next_request

    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        """Fetch name and price for a single Ozon article.

        Returns either `MarketplaceProductData` or a `MarketplaceFetchFailure`
        marking one of `blocked`, `not_found`, `transport_error` or
        `bad_payload` - never `None`, never an exception escaping to the
        caller (`BaseMarketplaceClient` contract).
        """

        params = {"url": f"/product/{article}/"}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        try:
            # One deadline over every hop: connect, headers and body read.
            # httpx timeouts bound a single operation, not the whole request.
            async with asyncio.timeout(self.TIMEOUT_SECONDS):
                body = await self._fetch_body(article, params, headers)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (403, 429):
                logger.warning(
                    f"Ozon blocked the request (HTTP {status}) for article {article}"
                )
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.blocked,
                    detail=f"HTTP {status}",
                )
            logger.warning(f"Error HTTP {status} for Ozon article {article}")
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
            logger.warning(f"Couldn't get data for Ozon article {article}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.transport_error,
                detail=type(e).__name__,
            )
        except TimeoutError as e:
            logger.warning(f"Deadline exceeded for Ozon article {article}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.transport_error,
                detail=type(e).__name__,
            )

        if isinstance(body, MarketplaceFetchFailure):
            return body

        try:
            parsed = OzonEntrypointResponse.model_validate_json(body)
            states = parsed.widget_states

            selected: dict[str, str] = {}
            for component in (_HEADING, _PRICE, _SKU):
                state = _select_widget_state(states, component)
                if isinstance(state, _WidgetProblem):
                    detail = f"{component} {state.value}"
                    logger.error(f"Ozon article {article}: {detail}")
                    return MarketplaceFetchFailure(
                        reason=FetchFailureReason.bad_payload,
                        detail=detail,
                    )
                selected[component] = state
            heading_raw = selected[_HEADING]
            price_raw = selected[_PRICE]
            sku_raw = selected[_SKU]

            sku = OzonDetailSkuState.model_validate_json(sku_raw)
            if sku.copy_text != str(article):
                logger.error(f"Ozon payload article differs from requested {article}")
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.bad_payload,
                    detail="article mismatch",
                )

            price = OzonPriceState.model_validate_json(price_raw)
            if not price.is_available:
                logger.error(
                    f"Ozon article {article}: isAvailable is false (unobserved)"
                )
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.bad_payload,
                    detail="isAvailable is false",
                )

            heading = OzonProductHeadingState.model_validate_json(heading_raw)
            return MarketplaceProductData(name=heading.title, price=price.price)
        except ValidationError as e:
            # A closed, named exception set: the `model_validate_json` calls
            # and `MarketplaceProductData(...)` are the only calls here that
            # can raise, and all raise `pydantic.ValidationError` (a non-JSON
            # body too). `str(e)` is never used - it echoes the input value.
            logger.error(f"Bad payload for Ozon article {article}: {type(e).__name__}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload,
                detail=type(e).__name__,
            )
