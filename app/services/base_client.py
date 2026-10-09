"""Base client for any marketplace."""

from abc import ABC
from abc import abstractmethod

import httpx

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult


async def read_body_bounded(
    response: httpx.Response, max_bytes: int
) -> bytes | MarketplaceFetchFailure:
    """Read a streamed response body, aborting once it exceeds `max_bytes`.

    The threshold is applied to decoded bytes (`aiter_bytes`), and reading
    stops at the first chunk that crosses it, so at most `ceil(max_bytes / C) + 1`
    chunks of size `C` are pulled. Peak memory: on failure `max_bytes + C`; on
    success up to `2 * max_bytes` (the accumulating `bytearray` plus the final
    `bytes` copy).
    An oversized body yields `bad_payload`; `detail` names the threshold and
    never carries body bytes. `httpx` errors raised while reading are not
    caught here - the calling client classifies them. Does not log: only the
    caller knows the article context.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > max_bytes:
            return MarketplaceFetchFailure(
                reason=FetchFailureReason.bad_payload,
                detail=f"body exceeds {max_bytes} bytes",
            )
    return bytes(body)


class BaseMarketplaceClient(ABC):
    """Base implementation for any Marketplace."""

    @abstractmethod
    async def get_product_data(self, article: int) -> MarketplaceFetchResult:
        """Get name and price for a tracked product.

        Contract (PROJECT.md section 2.6): an implementation always returns
        either `MarketplaceProductData` on success or a marked
        `MarketplaceFetchFailure` describing why the fetch did not succeed.
        Returning `None`, or letting a transport-level exception escape to
        the caller, are both contract violations.
        """
        raise NotImplementedError
