"""Ozon widget states are JSON strings inside the entrypoint JSON response.

The contract comes from tests/fixtures/ozon/entrypoint_3593896354.json.
Track cardPrice when available, otherwise price; values are in kopecks.
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any
from typing import cast

from curl_cffi.requests import AsyncSession
from curl_cffi.requests import Response
from curl_cffi.requests import exceptions as curl_errors
from curl_cffi.requests.exceptions import RequestException
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
from app.services.ozon_access import OzonAccess
from app.services.ozon_access import OzonAccessProvider
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_access import OzonAccessUnavailable
from app.services.socks_relay import SocksProxy

_HEADING = "webProductHeading"
_PRICE = "webPrice"
_SKU = "webDetailSKU"


class _WidgetProblem(Enum):
    missing = "missing"
    ambiguous = "ambiguous"


def _select_widget_state(
    widget_states: dict[str, str], component: str
) -> str | _WidgetProblem:
    """Require one exact component; prefixes may name different widgets."""
    found: str | None = None
    for key, state in widget_states.items():
        if key.partition("-")[0] != component:
            continue
        if found is not None:
            return _WidgetProblem.ambiguous
        found = state
    return _WidgetProblem.missing if found is None else found


class _BodyTooLarge(Exception):
    """A 2xx body crossed the size cap while it was being read."""


@dataclass(frozen=True, slots=True)
class OzonReply:
    """Status of a response and, for 2xx only, its body (bounded)."""

    status: int
    body: bytes | None


@dataclass(frozen=True, slots=True)
class OzonRedirect:
    """Status of a response and its `Location` header (3xx only); no body."""

    status: int
    location: str | None


class OzonSession:
    """Reuse a Chrome TLS fingerprint; Ozon rejects ordinary HTTP clients.

    Disable redirects and cookie persistence. Explicit proxies override libcurl
    environment proxies; socks5h resolves DNS through the configured proxy.
    """

    def __init__(self, proxy: SocksProxy | None = None) -> None:
        self._session = AsyncSession(
            impersonate="chrome",
            trust_env=False,
            proxies={"all": "" if proxy is None else proxy.curl_url},
            discard_cookies=True,
            allow_redirects=False,
        )

    async def _send(self, url: str, **kwargs: Any) -> Response:
        """Retry a dropped connection once, but not a timeout."""
        try:
            return await self._session.request("GET", url, **kwargs)
        except curl_errors.ConnectionError, curl_errors.ProxyError:
            return await self._session.request("GET", url, **kwargs)

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        access: OzonAccess,
        timeout_seconds: float,
        max_body_bytes: int,
    ) -> OzonReply:
        """Abort streams on exit: closing alone would drain even non-2xx bodies."""
        response = await self._send(
            url,
            params=params,
            headers={"User-Agent": access.user_agent},
            cookies=access.request_cookies(),
            timeout=timeout_seconds,
            stream=True,
        )
        try:
            status = int(response.status_code)
            if not 200 <= status < 300:
                return OzonReply(status, None)
            body = bytearray()
            # `curl_cffi` leaves these stream methods unannotated.
            async for chunk in response.aiter_content():  # type: ignore[no-untyped-call]
                body.extend(chunk)
                if len(body) > max_body_bytes:
                    raise _BodyTooLarge
            return OzonReply(status, bytes(body))
        finally:
            abort = cast("asyncio.Event | None", response.quit_now)
            if abort is not None:
                abort.set()
            await response.aclose()  # type: ignore[no-untyped-call]

    async def get_redirect(self, url: str, *, timeout_seconds: float) -> OzonRedirect:
        """Return Location without following redirects or reading the body."""
        response = await self._send(url, timeout=timeout_seconds, stream=True)
        try:
            status = int(response.status_code)
            location: str | None = None
            if 300 <= status < 400:
                header = response.headers.get("Location")
                location = header if isinstance(header, str) and header else None
            return OzonRedirect(status, location)
        finally:
            abort = cast("asyncio.Event | None", response.quit_now)
            if abort is not None:
                abort.set()
            await response.aclose()  # type: ignore[no-untyped-call]

    async def aclose(self) -> None:
        await self._session.close()


class OzonClient(BaseMarketplaceClient):
    URL = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
    SHORT_LINK_URL = "https://ozon.ru/t/"
    TIMEOUT_SECONDS = 10.0
    MAX_BODY_BYTES = 4_194_304

    def __init__(self, session: OzonSession, access: OzonAccessProvider) -> None:
        self._session = session
        self._access = access

    async def _fetch_body(self, article: int) -> bytes | MarketplaceFetchFailure:
        """Renew access once on 3xx/403; keep renewed access after a second refusal."""
        attempt = 0
        while True:
            attempt += 1
            access = await self._access.get()
            if isinstance(access, OzonAccessUnavailable):
                if access.stage is OzonAccessStage.proxy_failed:
                    logger.warning(
                        f"Ozon proxy failed for article {article}: {access.detail}"
                    )
                    return MarketplaceFetchFailure(
                        reason=FetchFailureReason.transport_error,
                        detail=f"Ozon access unavailable: {access.stage}",
                    )
                logger.warning(
                    f"Ozon access unavailable for article {article}: {access.stage}"
                )
                return MarketplaceFetchFailure(
                    reason=FetchFailureReason.blocked,
                    detail=f"Ozon access unavailable: {access.stage}",
                )
            reply = await self._request(article, access)
            if isinstance(reply, MarketplaceFetchFailure):
                return reply
            status = reply.status
            if reply.body is not None:
                return reply.body
            if 300 <= status < 400 or status == 403:
                if attempt == 2:
                    logger.warning(
                        f"Ozon refused renewed access (HTTP {status}) "
                        f"for article {article}"
                    )
                    return MarketplaceFetchFailure(
                        reason=FetchFailureReason.blocked,
                        detail=f"HTTP {status}",
                    )
                await self._access.invalidate(access)
                logger.info(
                    f"Ozon refused access (HTTP {status}) for article {article}, "
                    "renewing"
                )
                continue
            logger.warning(f"Error HTTP {status} for Ozon article {article}")
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

    async def _request(
        self, article: int, access: OzonAccess
    ) -> OzonReply | MarketplaceFetchFailure:
        try:
            async with asyncio.timeout(self.TIMEOUT_SECONDS):
                return await self._session.get(
                    self.URL,
                    params={"url": f"/product/{article}/"},
                    access=access,
                    timeout_seconds=self.TIMEOUT_SECONDS,
                    max_body_bytes=self.MAX_BODY_BYTES,
                )
        except _BodyTooLarge:
            detail = f"body exceeds {self.MAX_BODY_BYTES} bytes"
            logger.error(f"Oversized body for Ozon article {article}: {detail}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload, detail=detail
            )
        except RequestException as e:
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

    async def resolve_short_link(self, code: str) -> str | MarketplaceFetchFailure:
        """Return Location without following it; the domain layer validates the target.

        The fixed host and validated code prevent SSRF; neither code nor
        Location is logged.
        """
        if not (code.isascii() and code.isalnum() and 1 <= len(code) <= 32):
            logger.error("Ozon short link code rejected before the request")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload, detail="invalid code"
            )
        try:
            async with asyncio.timeout(self.TIMEOUT_SECONDS):
                redirect = await self._session.get_redirect(
                    f"{self.SHORT_LINK_URL}{code}",
                    timeout_seconds=self.TIMEOUT_SECONDS,
                )
        except (RequestException, TimeoutError) as e:
            logger.warning(f"Couldn't expand Ozon short link: {type(e).__name__}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.transport_error,
                detail=type(e).__name__,
            )
        status = redirect.status
        if 300 <= status < 400 and redirect.location is not None:
            return redirect.location
        detail = f"HTTP {status}"
        if status in (403, 429):
            logger.warning(f"Ozon refused short link expansion ({detail})")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.blocked, detail=detail
            )
        if status == 404:
            logger.info(f"Ozon short link not found ({detail})")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.not_found, detail=detail
            )
        if 200 <= status < 400:
            logger.error(f"Ozon short link gave no redirect ({detail})")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload, detail=detail
            )
        logger.warning(f"Error {detail} expanding Ozon short link")
        return MarketplaceFetchFailure(
            reason=FetchFailureReason.transport_error, detail=detail
        )

    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        body = await self._fetch_body(article)

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
            tracked = price.card_price if price.card_price is not None else price.price
            return MarketplaceProductData(name=heading.title, price=tracked)
        except ValidationError as e:
            # Validation errors may contain the upstream input.
            logger.error(f"Bad payload for Ozon article {article}: {type(e).__name__}")
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload,
                detail=type(e).__name__,
            )
