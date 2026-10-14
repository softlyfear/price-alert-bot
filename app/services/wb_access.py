"""Access to Wildberries by the anti-bot token (`PROJECT.md` section 2.12).

WB answers its price API only to a request that carries the cookie
`x_wbaas_token` (obtained by passing the anti-bot check in a browser) and any
`deviceid` header. This module owns the pair (`WbAccess`), the marked refusal
(`WbAccessUnavailable`), the provider contract `WbAccessProvider` that
`WbClient` depends on, and `BrowserWbAccessProvider` - a lazy cache of one
pair per process with storm protection (a pause after a failed acquisition
and a budget of acquisitions per window).

Secrets: the token and `deviceid` never reach a log record, a `detail` or a
`repr` - `WbAccess.__repr__` masks both, and `request_headers` returns values
whose own `repr` is masked, so they stay hidden in `--showlocals` output too.
"""

import asyncio
import secrets
import time
from collections import deque
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from loguru import logger

_MASK = "***"


class _MaskedStr(str):
    """A `str` whose `repr` hides the value (traceback locals, `pytest -l`)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return repr(_MASK)


@dataclass(frozen=True, slots=True, repr=False)
class WbAccess:
    """The pair a WB request needs: anti-bot token and `deviceid`."""

    token: str
    device_id: str

    def __repr__(self) -> str:
        return f"WbAccess(token='{_MASK}', device_id='{_MASK}')"

    def request_headers(self) -> dict[str, str]:
        """Headers carrying the pair; the cookie goes as a header, not into
        the shared `httpx` cookie jar that Ozon requests use as well."""
        return {
            "Cookie": _MaskedStr(f"x_wbaas_token={self.token}"),
            "deviceid": _MaskedStr(self.device_id),
        }


class WbAccessStage(StrEnum):
    launch_failed = "launch_failed"
    challenge_timeout = "challenge_timeout"
    paused = "paused"
    budget_exhausted = "budget_exhausted"
    closed = "closed"


@dataclass(frozen=True, slots=True)
class WbAccessUnavailable:
    """Marked refusal: no pair could be provided. `detail` carries no secrets."""

    stage: WbAccessStage
    detail: str


class WbTokenFetchError(Exception):
    """Raised by a token fetcher; carries the stage the failure belongs to."""

    def __init__(self, stage: WbAccessStage, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


class WbAccessProvider(Protocol):
    async def get(self) -> WbAccess | WbAccessUnavailable:
        """Return the current pair, acquiring one if there is none.

        Never raises (except `CancelledError` of the caller): a failure is
        a `WbAccessUnavailable`.
        """
        ...

    async def invalidate(self, access: WbAccess) -> None:
        """Drop `access` if it is still the current pair; otherwise no-op."""
        ...


class BrowserWbAccessProvider:
    """Lazy one-pair-per-process provider over a token fetcher (a browser).

    The acquisition is a task owned by the provider, not by the first caller:
    all concurrent `get` calls await one result, and cancelling one waiter
    does not cancel the acquisition for the others. All state is touched
    without an `await` between check and update, so no lock is needed on one
    event loop.
    """

    def __init__(
        self,
        fetch_token: Callable[[], Awaitable[str]],
        *,
        token_timeout_seconds: float,
        failure_pause_seconds: float,
        launch_budget: int,
        launch_window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch_token = fetch_token
        self._token_timeout = token_timeout_seconds
        self._failure_pause = failure_pause_seconds
        self._launch_budget = launch_budget
        self._launch_window = launch_window_seconds
        self._clock = clock
        self._current: WbAccess | None = None
        self._task: asyncio.Task[WbAccess | WbAccessUnavailable] | None = None
        self._launches: deque[float] = deque()
        self._pause_until: float | None = None
        self._pause_logged = False
        self._budget_logged = False
        self._closed = False

    async def get(self) -> WbAccess | WbAccessUnavailable:
        if self._closed:
            return WbAccessUnavailable(WbAccessStage.closed, "provider closed")
        if self._current is not None:
            return self._current
        if self._task is None:
            refusal = self._refusal()
            if refusal is not None:
                return refusal
            self._launches.append(self._clock())
            self._task = asyncio.create_task(self._acquire())
        # Shield: cancelling this caller must not cancel the shared task.
        return await asyncio.shield(self._task)

    async def invalidate(self, access: WbAccess) -> None:
        if self._current == access:
            self._current = None

    async def aclose(self) -> None:
        """Cancel a running acquisition; later `get` calls answer `closed`."""
        if self._closed:
            return
        self._closed = True
        self._current = None
        logger.info("WB access provider closed")
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.wait({task})

    def _refusal(self) -> WbAccessUnavailable | None:
        """Pause and budget checks before starting an acquisition."""
        now = self._clock()
        if self._pause_until is not None and now < self._pause_until:
            if not self._pause_logged:
                self._pause_logged = True
                logger.warning("WB token acquisition paused after a failure")
            return WbAccessUnavailable(WbAccessStage.paused, "pause after failure")
        while self._launches and now - self._launches[0] >= self._launch_window:
            self._launches.popleft()
        if len(self._launches) >= self._launch_budget:
            if not self._budget_logged:
                self._budget_logged = True
                logger.error(
                    f"WB token launch budget exhausted: {self._launch_budget} "
                    f"per {self._launch_window:g} s"
                )
            return WbAccessUnavailable(
                WbAccessStage.budget_exhausted, "launch budget exhausted"
            )
        self._budget_logged = False
        return None

    async def _acquire(self) -> WbAccess | WbAccessUnavailable:
        started = self._clock()
        failure: WbAccessUnavailable
        try:
            deadline = asyncio.timeout(self._token_timeout)
            try:
                async with deadline:
                    token = await self._fetch_token()
            except TimeoutError:
                if deadline.expired():
                    failure = WbAccessUnavailable(
                        WbAccessStage.challenge_timeout,
                        f"no token within {self._token_timeout:g} s",
                    )
                else:
                    failure = WbAccessUnavailable(
                        WbAccessStage.launch_failed, "TimeoutError"
                    )
            except WbTokenFetchError as e:
                failure = WbAccessUnavailable(e.stage, e.detail)
            except Exception as e:  # noqa: BLE001 - contract: never raises
                failure = WbAccessUnavailable(
                    WbAccessStage.launch_failed, type(e).__name__
                )
            else:
                access = WbAccess(token, "site_" + secrets.token_hex(16))
                self._current = access
                self._pause_until = None
                self._pause_logged = False
                logger.info(f"WB token acquired in {self._clock() - started:.1f} s")
                return access
            self._on_failure(failure)
            return failure
        except asyncio.CancelledError:
            if self._closed:
                return WbAccessUnavailable(WbAccessStage.closed, "provider closed")
            raise
        finally:
            self._task = None

    def _on_failure(self, failure: WbAccessUnavailable) -> None:
        self._pause_until = self._clock() + self._failure_pause
        self._pause_logged = False
        message = f"WB token acquisition failed: {failure.stage}, {failure.detail}"
        if failure.stage is WbAccessStage.challenge_timeout:
            logger.warning(message)
        else:
            logger.error(message)
