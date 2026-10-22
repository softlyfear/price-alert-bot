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

Access (`PROJECT.md` section 2.14): Ozon answers only a request that carries
the cookies of a passed anti-bot check and the UA of the browser that passed
it. They come from an `OzonAccessProvider` (a browser, once per process); the
price request itself is made by `curl_cffi` with the Chrome TLS fingerprint
(`httpx` is answered `307`/`403` - measured). Status map (O4):

- provider refusal: `blocked`, no request; `429`: `blocked`, access kept;
  the stage `proxy_failed` (the proxy refused, not Ozon) is `transport_error`;
- `3xx` or `403`: the access is invalidated and the request repeated once
  with a fresh one; a second `3xx`/`403` is `blocked` and does not
  invalidate the renewed access (a repeated refusal says nothing against it,
  and dropping it would start a browser on every article);
- `404`: `not_found`; other statuses and `curl_cffi` errors:
  `transport_error` (`detail` is `HTTP <status>` or the class name).

Anti-bot is an expected mode, reported as `blocked`, never as `not_found`.
Redirects are not followed (a `3xx` is the refusal signal, so no request goes
to a foreign address), the body of a non-2xx response is never read, the body
of a 2xx one is read with a size cap, cookies set by a response are
discarded (the request is a function of access and article, no hidden state),
and proxy environment variables are not read (the only proxy is the
configured SOCKS5 one, `socks5h`: DNS on the proxy side). Each attempt is bounded by one
deadline. Access cookies are a secret: they are masked in `repr`, never
logged and never put into `detail`.
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import cast

from curl_cffi.requests import AsyncSession
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
    """One `curl_cffi` `AsyncSession` per process (O7), owned by `lifespan`.

    No cookie jar (`discard_cookies`), no redirects, no proxy from the
    environment (`proxies={"all": ""}` makes libcurl ignore `ALL_PROXY`,
    `HTTPS_PROXY`, ...; `trust_env` alone does not), Chrome fingerprint,
    TLS verification on. With a `proxy` the whole traffic goes through it by
    `socks5h` (O10); the credentials are held in the libcurl session.
    """

    def __init__(self, proxy: SocksProxy | None = None) -> None:
        self._session = AsyncSession(
            impersonate="chrome",
            trust_env=False,
            proxies={"all": "" if proxy is None else proxy.curl_url},
            discard_cookies=True,
            allow_redirects=False,
        )

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        access: OzonAccess,
        timeout_seconds: float,
        max_body_bytes: int,
    ) -> OzonReply:
        """GET with the cookies and UA of `access`; a 2xx body is read, bounded.

        Raises `RequestException` (any `curl_cffi` failure) or
        `_BodyTooLarge`; `CancelledError` passes through. The stream is
        aborted on every exit: after the status of a non-2xx response is
        known, its body is not read (aborting stops the download at the next
        chunk; closing without the abort would drain it).
        """
        response = await self._session.request(
            "GET",
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
        """GET `url` without access cookies; report status and `Location`.

        No redirect is followed (the session forbids it) and the body is never
        read: the stream is aborted on every exit. Raises `RequestException`;
        `CancelledError` passes through.
        """
        response = await self._session.request(
            "GET", url, timeout=timeout_seconds, stream=True
        )
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
        """Request `article` with a provider access; on `3xx`/`403` renew once.

        Returns the 2xx body or a marked failure; never raises (apart from
        the caller's `CancelledError`).
        """
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
        """One HTTP attempt under one deadline; a failure is returned, not raised."""
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
        """Expand `ozon.ru/t/<code>` with one request; return its `Location`.

        The URL is built from the constant host and the validated `code` only
        (no SSRF); no access cookies, no browser, no redirect followed, the
        access provider is never called (PROJECT.md section 2.15, R22). The
        `Location` is returned as is: checking it is the domain's job. Neither
        the code nor the `Location` is logged (derived from user input).
        Status map: `3xx` with `Location` -> the string; `3xx` without it and
        `2xx` -> `bad_payload`; `403`/`429` -> `blocked`; `404` -> `not_found`;
        other statuses, `curl_cffi` errors and a missed deadline ->
        `transport_error`. Never raises (apart from `CancelledError`).
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
        """Fetch name and price for a single Ozon article.

        Returns either `MarketplaceProductData` or a `MarketplaceFetchFailure`
        marking one of `blocked`, `not_found`, `transport_error` or
        `bad_payload` - never `None`, never an exception escaping to the
        caller (`BaseMarketplaceClient` contract).
        """
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
