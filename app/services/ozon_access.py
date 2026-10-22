"""Access to Ozon by the cookies of a passed anti-bot check (`PROJECT.md` 2.14).

Ozon answers its price API only to a request that carries the cookies a
browser got by passing the anti-bot check (`__Secure-access-token` among them)
and the User-Agent of that browser. This module owns the pair (`OzonAccess`),
the marked refusal (`OzonAccessUnavailable`), the provider contract
`OzonAccessProvider` that `OzonClient` depends on, and
`BrowserOzonAccessProvider` - a lazy cache of one access per process with
storm protection (a pause after a failed acquisition and a budget of
acquisitions per window).

The provider mechanism repeats `BrowserWbAccessProvider` on purpose (O2): a
generic provider would rewrite the accepted WB one. The trigger to extract it:
a defect fixed in one provider that applies to the other, or a third consumer.

Secrets: cookie values never reach a log record, a `detail` or a `repr` -
`OzonAccess.__repr__` masks them, and `request_cookies` returns values whose
own `repr` is masked, so they stay hidden in `--showlocals` output too.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from loguru import logger

_MASK = "***"


class MaskedStr(str):
    """A `str` whose `repr` hides the value (traceback locals, `pytest -l`)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return repr(_MASK)


@dataclass(frozen=True, slots=True, repr=False)
class OzonAccess:
    """What an Ozon request needs: cookies of `ozon.ru` and the browser's UA."""

    cookies: tuple[tuple[str, str], ...]
    user_agent: str

    def __repr__(self) -> str:
        return f"OzonAccess(cookies=<{len(self.cookies)} masked>, user_agent=...)"

    def request_cookies(self) -> dict[str, str]:
        """Cookies for one request, values with a masked `repr`.

        Passed explicitly per request; the transport keeps no cookie jar.
        """
        return {name: MaskedStr(value) for name, value in self.cookies}


class OzonAccessStage(StrEnum):
    launch_failed = "launch_failed"
    challenge_timeout = "challenge_timeout"
    paused = "paused"
    budget_exhausted = "budget_exhausted"
    closed = "closed"


@dataclass(frozen=True, slots=True)
class OzonAccessUnavailable:
    """Marked refusal: no access could be provided. `detail` carries no secrets."""

    stage: OzonAccessStage
    detail: str


class OzonAccessFetchError(Exception):
    """Raised by an access fetcher; carries the stage the failure belongs to."""

    def __init__(self, stage: OzonAccessStage, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


class OzonAccessProvider(Protocol):
    async def get(self) -> OzonAccess | OzonAccessUnavailable:
        """Return the current access, acquiring one if there is none.

        Never raises (except `CancelledError` of the caller): a failure is
        an `OzonAccessUnavailable`.
        """
        ...

    async def invalidate(self, access: OzonAccess) -> None:
        """Drop `access` if it is still the current one; otherwise no-op."""
        ...


class BrowserOzonAccessProvider:
    """Lazy one-access-per-process provider over an access fetcher (a browser).

    The acquisition is a task owned by the provider, not by the first caller:
    all concurrent `get` calls await one result, and cancelling one waiter
    does not cancel the acquisition for the others. All state is touched
    without an `await` between check and update, so no lock is needed on one
    event loop.
    """

    def __init__(
        self,
        fetch_access: Callable[[], Awaitable[OzonAccess]],
        *,
        token_timeout_seconds: float,
        failure_pause_seconds: float,
        launch_budget: int,
        launch_window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch_access = fetch_access
        self._token_timeout = token_timeout_seconds
        self._failure_pause = failure_pause_seconds
        self._launch_budget = launch_budget
        self._launch_window = launch_window_seconds
        self._clock = clock
        self._current: OzonAccess | None = None
        self._task: asyncio.Task[OzonAccess | OzonAccessUnavailable] | None = None
        self._launches: deque[float] = deque()
        self._pause_until: float | None = None
        self._pause_logged = False
        self._budget_logged = False
        self._closed = False

    async def get(self) -> OzonAccess | OzonAccessUnavailable:
        if self._closed:
            return OzonAccessUnavailable(OzonAccessStage.closed, "provider closed")
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

    async def invalidate(self, access: OzonAccess) -> None:
        if self._current == access:
            self._current = None

    async def aclose(self) -> None:
        """Cancel a running acquisition; later `get` calls answer `closed`."""
        if self._closed:
            return
        self._closed = True
        self._current = None
        logger.info("Ozon access provider closed")
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.wait({task})

    def _refusal(self) -> OzonAccessUnavailable | None:
        """Pause and budget checks before starting an acquisition."""
        now = self._clock()
        if self._pause_until is not None and now < self._pause_until:
            if not self._pause_logged:
                self._pause_logged = True
                logger.warning("Ozon access acquisition paused after a failure")
            return OzonAccessUnavailable(OzonAccessStage.paused, "pause after failure")
        while self._launches and now - self._launches[0] >= self._launch_window:
            self._launches.popleft()
        if len(self._launches) >= self._launch_budget:
            if not self._budget_logged:
                self._budget_logged = True
                logger.error(
                    f"Ozon access launch budget exhausted: {self._launch_budget} "
                    f"per {self._launch_window:g} s"
                )
            return OzonAccessUnavailable(
                OzonAccessStage.budget_exhausted, "launch budget exhausted"
            )
        self._budget_logged = False
        return None

    async def _acquire(self) -> OzonAccess | OzonAccessUnavailable:
        started = self._clock()
        failure: OzonAccessUnavailable
        try:
            deadline = asyncio.timeout(self._token_timeout)
            try:
                async with deadline:
                    access = await self._fetch_access()
            except TimeoutError:
                if deadline.expired():
                    failure = OzonAccessUnavailable(
                        OzonAccessStage.challenge_timeout,
                        f"no access within {self._token_timeout:g} s",
                    )
                else:
                    failure = OzonAccessUnavailable(
                        OzonAccessStage.launch_failed, "TimeoutError"
                    )
            except OzonAccessFetchError as e:
                failure = OzonAccessUnavailable(e.stage, e.detail)
            except Exception as e:  # noqa: BLE001 - contract: never raises
                failure = OzonAccessUnavailable(
                    OzonAccessStage.launch_failed, type(e).__name__
                )
            else:
                self._current = access
                self._pause_until = None
                self._pause_logged = False
                logger.info(f"Ozon access acquired in {self._clock() - started:.1f} s")
                return access
            self._on_failure(failure)
            return failure
        except asyncio.CancelledError:
            if self._closed:
                return OzonAccessUnavailable(OzonAccessStage.closed, "provider closed")
            raise
        finally:
            self._task = None

    def _on_failure(self, failure: OzonAccessUnavailable) -> None:
        self._pause_until = self._clock() + self._failure_pause
        self._pause_logged = False
        message = f"Ozon access acquisition failed: {failure.stage}, {failure.detail}"
        if failure.stage is OzonAccessStage.challenge_timeout:
            logger.warning(message)
        else:
            logger.error(message)
