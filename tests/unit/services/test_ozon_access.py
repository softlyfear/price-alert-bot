"""Unit tests for `app.services.ozon_access` (PAB-110 AC5, AC13).

No browser and no real time: the access fetcher is a hand-written fake that
returns, raises or hangs on a plan, and the provider's clock is a settable
number. The one real timer is the acquisition deadline of the
`challenge_timeout` cell (a few milliseconds against a fetcher that never
returns): the deadline is an `asyncio.timeout` by contract.

Synchronisation is by events, never by `sleep`; every wait for a task sits
under a 5 s fuse so a missed wake-up fails instead of hanging. Expected values
are literals of the ticket (stages, levels, messages), not recomputed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from app.services.ozon_access import BrowserOzonAccessProvider
from app.services.ozon_access import MaskedStr
from app.services.ozon_access import OzonAccess
from app.services.ozon_access import OzonAccessFetchError
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_access import OzonAccessUnavailable
from tests.unit.ozon_stubs import make_access

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_FUSE_SECONDS = 5.0
_HANG = object()

_Plan = OzonAccess | BaseException | object


class _Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Fetcher:
    """An access fetcher on a plan: an `OzonAccess` is returned, an exception
    raised, `_HANG` waits forever. The last plan repeats. `gate`, if given,
    holds the fetch before it acts; `started` is set on entry; `cancelled`
    counts the cancellations that reached the fetch."""

    def __init__(
        self,
        *plans: _Plan,
        gate: asyncio.Event | None = None,
        clock: _Clock | None = None,
        takes_seconds: float = 0.0,
    ) -> None:
        self._plans = list(plans) or [make_access(1)]
        self._gate = gate
        self._clock = clock
        self._takes = takes_seconds
        self.calls = 0
        self.cancelled = 0
        self.started = asyncio.Event()

    async def __call__(self) -> OzonAccess:
        plan = self._plans[min(self.calls, len(self._plans) - 1)]
        self.calls += 1
        self.started.set()
        try:
            if self._gate is not None:
                await self._gate.wait()
            if self._clock is not None:
                self._clock.now += self._takes
            if plan is _HANG:
                await asyncio.Event().wait()
            if isinstance(plan, BaseException):
                raise plan
            assert isinstance(plan, OzonAccess)
            return plan
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def _provider(
    fetch: Callable[[], Awaitable[OzonAccess]],
    clock: _Clock | None = None,
    *,
    token_timeout_seconds: float = 30.0,
    failure_pause_seconds: float = 300.0,
    launch_budget: int = 8,
    launch_window_seconds: float = 900.0,
) -> BrowserOzonAccessProvider:
    return BrowserOzonAccessProvider(
        fetch,
        token_timeout_seconds=token_timeout_seconds,
        failure_pause_seconds=failure_pause_seconds,
        launch_budget=launch_budget,
        launch_window_seconds=launch_window_seconds,
        clock=clock or _Clock(),
    )


async def _get(
    provider: BrowserOzonAccessProvider,
) -> OzonAccess | OzonAccessUnavailable:
    return await asyncio.wait_for(provider.get(), _FUSE_SECONDS)


async def _get_access(provider: BrowserOzonAccessProvider) -> OzonAccess:
    result = await _get(provider)
    assert isinstance(result, OzonAccess), result
    return result


def _capture() -> tuple[list[Record], int]:
    records: list[Record] = []

    def sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(sink, level=0)


def _levels(records: list[Record]) -> list[str]:
    return [r["level"].name for r in records]


# --- values ------------------------------------------------------------------


def test_ozon_access_repr_and_str_mask_every_cookie_value() -> None:
    access = OzonAccess(
        cookies=(
            ("__Secure-access-token", "COOKIE-MARKER-X"),
            ("a", "COOKIE-MARKER-Y"),
        ),
        user_agent="UA",
    )

    for text in (repr(access), str(access), f"{access}", f"{[access]}"):
        assert "COOKIE-MARKER" not in text
        assert "OzonAccess(" in text  # positive control: it is the access' repr


def test_ozon_access_is_a_frozen_value_compared_by_content() -> None:
    access = OzonAccess(cookies=(("a", "1"),), user_agent="UA")

    assert access == OzonAccess(cookies=(("a", "1"),), user_agent="UA")
    assert access != OzonAccess(cookies=(("a", "2"),), user_agent="UA")
    with pytest.raises(FrozenInstanceError):
        access.user_agent = "changed"  # type: ignore[misc]


def test_request_cookies_carry_the_real_values_with_a_masked_repr() -> None:
    """The dict is a local of the transport, printed by `pytest --showlocals`:
    neither the dict nor its values may show a marker, yet the value that goes
    on the wire is the real one."""
    cookies = OzonAccess(
        cookies=(("__Secure-access-token", "COOKIE-MARKER-X"),), user_agent="UA"
    ).request_cookies()

    assert cookies == {"__Secure-access-token": "COOKIE-MARKER-X"}
    assert type(cookies["__Secure-access-token"]) is MaskedStr
    assert str(cookies["__Secure-access-token"]) == "COOKIE-MARKER-X"
    for text in (repr(cookies), repr(cookies["__Secure-access-token"])):
        assert "COOKIE-MARKER-X" not in text
    assert repr(cookies["__Secure-access-token"]) == "'***'"


def test_unavailable_is_a_frozen_value_carrying_stage_and_detail() -> None:
    refusal = OzonAccessUnavailable(OzonAccessStage.paused, "pause after failure")

    assert refusal.stage is OzonAccessStage.paused
    assert refusal.detail == "pause after failure"
    with pytest.raises(FrozenInstanceError):
        refusal.detail = "x"  # type: ignore[misc]


def test_stage_values_are_the_five_names_of_the_contract() -> None:
    assert {s.value for s in OzonAccessStage} == {
        "launch_failed",
        "challenge_timeout",
        "paused",
        "budget_exhausted",
        "closed",
    }
    assert f"{OzonAccessStage.challenge_timeout}" == "challenge_timeout"


def test_fetch_error_carries_stage_and_detail() -> None:
    error = OzonAccessFetchError(OzonAccessStage.launch_failed, "no chromium")

    assert error.stage is OzonAccessStage.launch_failed
    assert error.detail == "no chromium"
    assert str(error) == "no chromium"


# --- AC5 (a), (b): one acquisition for all waiters -----------------------------


@pytest.mark.asyncio
async def test_five_concurrent_gets_share_one_acquisition_and_one_access() -> None:
    gate = asyncio.Event()
    fetch = _Fetcher(make_access(1), gate=gate)
    provider = _provider(fetch)

    waiters = [asyncio.create_task(provider.get()) for _ in range(5)]
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*waiters), _FUSE_SECONDS)

    assert fetch.calls == 1
    assert all(r == make_access(1) for r in results)


@pytest.mark.asyncio
async def test_cached_access_is_returned_without_a_new_acquisition() -> None:
    fetch = _Fetcher()
    provider = _provider(fetch)

    first = await _get_access(provider)
    second = await _get_access(provider)

    assert second is first
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_the_acquisition_for_others() -> (
    None
):
    gate = asyncio.Event()
    fetch = _Fetcher(make_access(1), gate=gate)
    provider = _provider(fetch)

    victim = asyncio.create_task(provider.get())
    survivor = asyncio.create_task(provider.get())
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(victim, _FUSE_SECONDS)
    gate.set()
    result = await asyncio.wait_for(survivor, _FUSE_SECONDS)

    assert result == make_access(1)
    assert fetch.calls == 1
    assert fetch.cancelled == 0


@pytest.mark.asyncio
async def test_cancelling_the_only_waiter_propagates_and_keeps_the_acquisition() -> (
    None
):
    gate = asyncio.Event()
    fetch = _Fetcher(make_access(1), gate=gate)
    provider = _provider(fetch)

    caller = asyncio.create_task(provider.get())
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, _FUSE_SECONDS)
    gate.set()
    access = await _get_access(provider)

    assert access == make_access(1)
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_cancellation_of_the_acquisition_itself_without_close_propagates() -> (
    None
):
    """A `CancelledError` that is not an `aclose` reaches the waiter as
    `CancelledError`, not as a refusal; no pause is set and the next `get`
    acquires."""
    fetch = _Fetcher(asyncio.CancelledError(), make_access(2))
    provider = _provider(fetch)

    with pytest.raises(asyncio.CancelledError):
        await _get(provider)
    access = await _get_access(provider)

    assert access == make_access(2)
    assert fetch.calls == 2


# --- AC5 (c): invalidate -----------------------------------------------------


@pytest.mark.asyncio
async def test_invalidating_the_current_access_makes_the_next_get_acquire_anew() -> (
    None
):
    fetch = _Fetcher(make_access(1), make_access(2))
    provider = _provider(fetch)
    first = await _get_access(provider)

    await provider.invalidate(first)
    second = await _get_access(provider)

    assert (first, second) == (make_access(1), make_access(2))
    assert fetch.calls == 2


@pytest.mark.asyncio
async def test_invalidating_a_stale_access_does_not_start_an_acquisition() -> None:
    """Two callers hold access 1; the first renews it. The second's late
    `invalidate(1)` must neither drop access 2 nor launch a browser."""
    fetch = _Fetcher(make_access(1), make_access(2))
    provider = _provider(fetch)
    stale = await _get_access(provider)
    await provider.invalidate(stale)
    current = await _get_access(provider)

    await provider.invalidate(stale)
    again = await _get_access(provider)

    assert current == make_access(2)
    assert again is current
    assert fetch.calls == 2


@pytest.mark.asyncio
async def test_invalidate_before_any_acquisition_is_a_no_op() -> None:
    fetch = _Fetcher()
    provider = _provider(fetch)

    await provider.invalidate(make_access(9))

    assert fetch.calls == 0


# --- AC5 (d): pause after a failure ------------------------------------------


@pytest.mark.asyncio
async def test_after_a_failure_get_is_paused_until_the_pause_ends() -> None:
    clock = _Clock()
    fetch = _Fetcher(OzonAccessFetchError(OzonAccessStage.launch_failed, "boom"))
    provider = _provider(fetch, clock, failure_pause_seconds=300.0)

    first = await _get(provider)
    clock.now = 299.0
    during = await _get(provider)
    clock.now = 300.0
    after = await _get(provider)

    assert first == OzonAccessUnavailable(OzonAccessStage.launch_failed, "boom")
    assert during == OzonAccessUnavailable(
        OzonAccessStage.paused, "pause after failure"
    )
    assert fetch.calls == 2  # the call at t=299 did not acquire; t=300 did
    assert after == OzonAccessUnavailable(OzonAccessStage.launch_failed, "boom")


@pytest.mark.asyncio
async def test_pause_is_logged_once_per_failure_and_not_per_get() -> None:
    clock = _Clock()
    provider = _provider(
        _Fetcher(OzonAccessFetchError(OzonAccessStage.launch_failed, "boom")), clock
    )
    await _get(provider)
    records, sink_id = _capture()
    try:
        for _ in range(3):
            await _get(provider)
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["WARNING"]
    assert "paused" in records[0]["message"]


@pytest.mark.asyncio
async def test_success_after_the_pause_clears_it() -> None:
    clock = _Clock()
    fetch = _Fetcher(
        OzonAccessFetchError(OzonAccessStage.launch_failed, "boom"), make_access(2)
    )
    provider = _provider(fetch, clock)
    await _get(provider)
    clock.now = 301.0
    access = await _get_access(provider)
    await provider.invalidate(access)

    again = await _get(provider)  # no pause after a success: it acquires

    assert again == make_access(2)
    assert fetch.calls == 3


# --- AC5 (e): budget -----------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_refuses_without_acquiring_and_logs_error_once() -> None:
    clock = _Clock()
    fetch = _Fetcher(make_access(1), make_access(2), make_access(3))
    provider = _provider(fetch, clock, launch_budget=2, launch_window_seconds=900.0)
    for _ in range(2):
        await provider.invalidate(await _get_access(provider))
    records, sink_id = _capture()
    try:
        refusals = [await _get(provider) for _ in range(3)]
    finally:
        logger.remove(sink_id)

    assert (
        refusals
        == [
            OzonAccessUnavailable(
                OzonAccessStage.budget_exhausted, "launch budget exhausted"
            )
        ]
        * 3
    )
    assert fetch.calls == 2
    assert _levels(records) == ["ERROR"]
    assert "2 per 900 s" in records[0]["message"]


@pytest.mark.asyncio
async def test_budget_is_released_when_old_launches_leave_the_window() -> None:
    clock = _Clock()
    fetch = _Fetcher(make_access(1), make_access(2), make_access(3))
    provider = _provider(fetch, clock, launch_budget=2, launch_window_seconds=900.0)
    clock.now = 0.0
    await provider.invalidate(await _get_access(provider))
    clock.now = 500.0
    await provider.invalidate(await _get_access(provider))
    assert isinstance(await _get(provider), OzonAccessUnavailable)

    clock.now = 900.0  # the launch of t=0 is exactly one window old: it leaves
    access = await _get_access(provider)

    assert access == make_access(3)
    assert fetch.calls == 3


@pytest.mark.asyncio
async def test_budget_error_is_logged_again_for_a_new_exhaustion() -> None:
    clock = _Clock()
    fetch = _Fetcher(*(make_access(n) for n in range(1, 6)))
    provider = _provider(fetch, clock, launch_budget=1, launch_window_seconds=100.0)
    await provider.invalidate(await _get_access(provider))
    records, sink_id = _capture()
    try:
        await _get(provider)  # exhausted: ERROR #1
        clock.now = 100.0
        await provider.invalidate(await _get_access(provider))  # budget is free again
        await _get(provider)  # exhausted again: ERROR #2
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["ERROR", "INFO", "ERROR"]


# --- AC5 (f): close ------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_cancels_a_running_acquisition_and_waiters_get_closed() -> None:
    fetch = _Fetcher(_HANG)
    provider = _provider(fetch)
    waiter = asyncio.create_task(provider.get())
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)

    await asyncio.wait_for(provider.aclose(), _FUSE_SECONDS)
    result = await asyncio.wait_for(waiter, _FUSE_SECONDS)

    assert fetch.cancelled == 1
    assert result == OzonAccessUnavailable(OzonAccessStage.closed, "provider closed")


@pytest.mark.asyncio
async def test_get_after_aclose_is_closed_even_with_a_cached_access() -> None:
    fetch = _Fetcher()
    provider = _provider(fetch)
    await _get_access(provider)

    await provider.aclose()
    result = await _get(provider)

    assert result == OzonAccessUnavailable(OzonAccessStage.closed, "provider closed")
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_aclose_is_idempotent_and_logs_once() -> None:
    provider = _provider(_Fetcher())
    records, sink_id = _capture()
    try:
        await provider.aclose()
        await provider.aclose()
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["INFO"]


# --- AC5 (g): outcomes and their levels ----------------------------------------


@pytest.mark.asyncio
async def test_deadline_is_challenge_timeout_with_a_warning() -> None:
    fetch = _Fetcher(_HANG)
    provider = _provider(fetch, token_timeout_seconds=0.01)
    records, sink_id = _capture()
    try:
        result = await _get(provider)
    finally:
        logger.remove(sink_id)

    assert result == OzonAccessUnavailable(
        OzonAccessStage.challenge_timeout, "no access within 0.01 s"
    )
    assert fetch.cancelled == 1
    assert _levels(records) == ["WARNING"]


@pytest.mark.asyncio
async def test_a_timeout_raised_by_the_fetcher_itself_is_launch_failed() -> None:
    """A `TimeoutError` of the fetcher (not the provider's deadline) is a
    launch problem, not a silent challenge."""
    provider = _provider(_Fetcher(TimeoutError("inner")))
    records, sink_id = _capture()
    try:
        result = await _get(provider)
    finally:
        logger.remove(sink_id)

    assert result == OzonAccessUnavailable(
        OzonAccessStage.launch_failed, "TimeoutError"
    )
    assert _levels(records) == ["ERROR"]


@pytest.mark.asyncio
async def test_marked_fetch_error_keeps_its_stage_and_detail_with_an_error() -> None:
    provider = _provider(
        _Fetcher(
            OzonAccessFetchError(OzonAccessStage.launch_failed, "browser_not_installed")
        )
    )
    records, sink_id = _capture()
    try:
        result = await _get(provider)
    finally:
        logger.remove(sink_id)

    assert result == OzonAccessUnavailable(
        OzonAccessStage.launch_failed, "browser_not_installed"
    )
    assert _levels(records) == ["ERROR"]
    assert "browser_not_installed" in records[0]["message"]


@pytest.mark.asyncio
async def test_unexpected_exception_is_launch_failed_named_by_class_only() -> None:
    provider = _provider(_Fetcher(RuntimeError("SECRET-EXC-MARKER")))
    records, sink_id = _capture()
    try:
        result = await _get(provider)
    finally:
        logger.remove(sink_id)

    assert result == OzonAccessUnavailable(
        OzonAccessStage.launch_failed, "RuntimeError"
    )
    assert "SECRET-EXC-MARKER" not in str(records[0]["message"])
    assert "SECRET-EXC-MARKER" not in str(records[0]["extra"])


@pytest.mark.asyncio
async def test_success_logs_info_with_the_duration_and_no_cookie_values() -> None:
    clock = _Clock()
    provider = _provider(
        _Fetcher(make_access(1), clock=clock, takes_seconds=2.5), clock
    )
    records, sink_id = _capture()
    try:
        await _get_access(provider)
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["INFO"]
    assert records[0]["message"] == "Ozon access acquired in 2.5 s"
    assert "OZON-TOKEN-MARKER" not in records[0]["message"]


@pytest.mark.asyncio
async def test_no_log_or_detail_ever_holds_a_cookie_value() -> None:
    """Positive control: the marker is in the access, the logger captures
    records; across success, refusal and failure it is in none of them."""
    clock = _Clock()
    access = make_access(1)
    records, sink_id = _capture()
    try:
        ok = _provider(_Fetcher(access), clock)
        got = await _get_access(ok)
        await ok.invalidate(got)
        failing = _provider(
            _Fetcher(OzonAccessFetchError(OzonAccessStage.launch_failed, "x")), clock
        )
        refusal = await _get(failing)
        paused = await _get(failing)
    finally:
        logger.remove(sink_id)

    assert access.cookies[0][1] == "OZON-TOKEN-MARKER-1"  # the marker exists
    assert records  # the sink works
    blob = " ".join(str(r["message"]) + str(r["extra"]) for r in records)
    assert "OZON-TOKEN-MARKER" not in blob
    for unavailable in (refusal, paused):
        assert isinstance(unavailable, OzonAccessUnavailable)
        assert "OZON-TOKEN-MARKER" not in unavailable.detail


# --- AC5 (h): nothing but the caller's CancelledError leaves `get` -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("x"),
        ValueError("x"),
        OSError("x"),
        OzonAccessFetchError(OzonAccessStage.challenge_timeout, "x"),
    ],
    ids=["runtime", "value", "os", "marked"],
)
async def test_get_returns_a_refusal_for_any_fetcher_exception(
    error: Exception,
) -> None:
    result = await _get(_provider(_Fetcher(error)))

    assert isinstance(result, OzonAccessUnavailable)
