"""Unit tests for `app.services.wb_access` (PAB-079 AC4, AC6, AC7).

No browser and no real time: the token fetcher is a hand-written fake that
returns, raises or hangs on a plan, and the provider's clock is a settable
number. The one real timer is the acquisition deadline of the
`challenge_timeout` cells (`token_timeout_seconds` of a few milliseconds
against a fetcher that never returns) - the deadline is an `asyncio.timeout`
by contract, so it cannot be driven by the fake clock.

Synchronisation is by events (`started`, `gate`), never by `sleep`; every wait
for a task sits under a 5 s fuse so a missed wake-up fails instead of hanging.
Expected values are literals of the ticket (`site_` + 32 hex, stages, levels).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from app.services.wb_access import BrowserWbAccessProvider
from app.services.wb_access import WbAccess
from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbAccessUnavailable
from app.services.wb_access import WbTokenFetchError

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_DEVICE_ID = re.compile(r"^site_[0-9a-f]{32}$")
_FUSE_SECONDS = 5.0
_HANG = object()

_Plan = str | BaseException | object


class _Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Fetcher:
    """A token fetcher on a plan: a `str` is returned, an exception raised,
    `_HANG` waits forever. The last plan repeats. `gate`, if given, holds the
    fetch before it acts; `started` is set on entry; `cancelled` counts the
    cancellations that reached the fetch."""

    def __init__(
        self,
        *plans: _Plan,
        gate: asyncio.Event | None = None,
        clock: _Clock | None = None,
        takes_seconds: float = 0.0,
    ) -> None:
        self._plans = list(plans) or ["TOKEN-MARKER-1"]
        self._gate = gate
        self._clock = clock
        self._takes = takes_seconds
        self.calls = 0
        self.cancelled = 0
        self.started = asyncio.Event()

    async def __call__(self) -> str:
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
            assert isinstance(plan, str)
            return plan
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def _provider(
    fetch: Callable[[], Awaitable[str]],
    clock: _Clock | None = None,
    *,
    token_timeout_seconds: float = 30.0,
    failure_pause_seconds: float = 300.0,
    launch_budget: int = 8,
    launch_window_seconds: float = 900.0,
) -> BrowserWbAccessProvider:
    return BrowserWbAccessProvider(
        fetch,
        token_timeout_seconds=token_timeout_seconds,
        failure_pause_seconds=failure_pause_seconds,
        launch_budget=launch_budget,
        launch_window_seconds=launch_window_seconds,
        clock=clock or _Clock(),
    )


async def _get(provider: BrowserWbAccessProvider) -> WbAccess | WbAccessUnavailable:
    return await asyncio.wait_for(provider.get(), _FUSE_SECONDS)


async def _get_pair(provider: BrowserWbAccessProvider) -> WbAccess:
    result = await _get(provider)
    assert isinstance(result, WbAccess), result
    return result


def _capture() -> tuple[list[Record], int]:
    records: list[Record] = []

    def sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(sink, level=0)


def _levels(records: list[Record]) -> list[str]:
    return [r["level"].name for r in records]


# --- WbAccess / WbAccessUnavailable values -----------------------------------


def test_wb_access_repr_and_str_mask_both_values() -> None:
    access = WbAccess(token="TOKEN-MARKER-X", device_id="DEVICE-MARKER-X")

    for text in (repr(access), str(access), f"{access}", f"{[access]}"):
        assert "TOKEN-MARKER-X" not in text
        assert "DEVICE-MARKER-X" not in text
        assert "WbAccess(" in text  # positive control: it is the pair's repr


def test_wb_access_is_a_frozen_value_compared_by_content() -> None:
    access = WbAccess(token="t", device_id="d")

    assert access == WbAccess(token="t", device_id="d")
    assert access != WbAccess(token="t", device_id="other")
    with pytest.raises(FrozenInstanceError):
        access.token = "changed"  # type: ignore[misc]


def test_request_headers_carry_cookie_and_deviceid_with_the_real_values() -> None:
    headers = WbAccess(
        token="TOKEN-MARKER-X", device_id="DEVICE-MARKER-X"
    ).request_headers()

    assert headers == {
        "Cookie": "x_wbaas_token=TOKEN-MARKER-X",
        "deviceid": "DEVICE-MARKER-X",
    }
    assert type(headers["Cookie"]) is not str  # the masked subclass
    assert isinstance(headers["Cookie"], str)


def test_request_headers_values_and_dict_have_masked_repr() -> None:
    """The headers dict is a local of `WbClient._request`, printed by
    `pytest --showlocals`: neither the dict nor the values may show a marker."""
    headers = WbAccess(
        token="TOKEN-MARKER-X", device_id="DEVICE-MARKER-X"
    ).request_headers()

    for text in (repr(headers), repr(headers["Cookie"]), repr(headers["deviceid"])):
        assert "TOKEN-MARKER-X" not in text
        assert "DEVICE-MARKER-X" not in text
    assert repr(headers["Cookie"]) == "'***'"
    assert repr(headers["deviceid"]) == "'***'"


def test_unavailable_is_a_frozen_value_carrying_stage_and_detail() -> None:
    refusal = WbAccessUnavailable(WbAccessStage.paused, "pause after failure")

    assert refusal.stage is WbAccessStage.paused
    assert refusal.detail == "pause after failure"
    with pytest.raises(FrozenInstanceError):
        refusal.detail = "x"  # type: ignore[misc]


def test_stage_values_are_the_five_names_of_the_contract() -> None:
    assert {s.value for s in WbAccessStage} == {
        "launch_failed",
        "challenge_timeout",
        "paused",
        "budget_exhausted",
        "closed",
    }
    assert f"{WbAccessStage.challenge_timeout}" == "challenge_timeout"


def test_token_fetch_error_carries_stage_and_detail() -> None:
    error = WbTokenFetchError(WbAccessStage.launch_failed, "no chromium")

    assert error.stage is WbAccessStage.launch_failed
    assert error.detail == "no chromium"
    assert str(error) == "no chromium"


# --- AC4 (a), (b): one acquisition for all waiters ---------------------------


@pytest.mark.asyncio
async def test_five_concurrent_gets_share_one_acquisition_and_one_pair() -> None:
    gate = asyncio.Event()
    fetch = _Fetcher("TOKEN-MARKER-1", gate=gate)
    provider = _provider(fetch)

    waiters = [asyncio.create_task(provider.get()) for _ in range(5)]
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*waiters), _FUSE_SECONDS)

    assert fetch.calls == 1
    first = results[0]
    assert isinstance(first, WbAccess)
    assert first.token == "TOKEN-MARKER-1"
    assert all(r is first for r in results)


@pytest.mark.asyncio
async def test_pair_has_site_prefix_and_32_hex_device_id() -> None:
    pair = await _get_pair(_provider(_Fetcher()))

    assert _DEVICE_ID.match(pair.device_id)


@pytest.mark.asyncio
async def test_cached_pair_is_returned_without_a_new_acquisition() -> None:
    fetch = _Fetcher()
    provider = _provider(fetch)

    first = await _get_pair(provider)
    second = await _get_pair(provider)

    assert second is first
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_the_acquisition_for_others() -> (
    None
):
    gate = asyncio.Event()
    fetch = _Fetcher("TOKEN-MARKER-1", gate=gate)
    provider = _provider(fetch)

    victim = asyncio.create_task(provider.get())
    survivor = asyncio.create_task(provider.get())
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(victim, _FUSE_SECONDS)
    gate.set()
    result = await asyncio.wait_for(survivor, _FUSE_SECONDS)

    assert isinstance(result, WbAccess)
    assert result.token == "TOKEN-MARKER-1"
    assert fetch.calls == 1
    assert fetch.cancelled == 0


@pytest.mark.asyncio
async def test_cancelling_the_only_waiter_propagates_and_keeps_the_acquisition() -> (
    None
):
    """AC4(ж): the caller's `CancelledError` is not swallowed (the awaiting
    task ends cancelled); the acquisition it started is not lost - a later
    `get` receives its result without a second launch."""
    gate = asyncio.Event()
    fetch = _Fetcher("TOKEN-MARKER-1", gate=gate)
    provider = _provider(fetch)

    caller = asyncio.create_task(provider.get())
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, _FUSE_SECONDS)
    assert caller.cancelled()
    gate.set()
    pair = await _get_pair(provider)

    assert pair.token == "TOKEN-MARKER-1"
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_cancellation_of_the_acquisition_itself_without_close_propagates() -> (
    None
):
    """A `CancelledError` that is not an `aclose` (the fetch task cancelled
    from outside) reaches the waiter as `CancelledError`, not as a refusal;
    the provider is not closed, sets no pause and the next `get` acquires."""
    fetch = _Fetcher(asyncio.CancelledError(), "TOKEN-MARKER-2")
    provider = _provider(fetch)

    with pytest.raises(asyncio.CancelledError):
        await _get(provider)
    pair = await _get_pair(provider)

    assert pair.token == "TOKEN-MARKER-2"
    assert fetch.calls == 2


# --- AC4 (c): invalidate -----------------------------------------------------


@pytest.mark.asyncio
async def test_invalidating_the_current_pair_makes_the_next_get_acquire_a_new_one() -> (
    None
):
    fetch = _Fetcher("TOKEN-MARKER-1", "TOKEN-MARKER-2")
    provider = _provider(fetch)
    first = await _get_pair(provider)

    await provider.invalidate(first)
    second = await _get_pair(provider)

    assert fetch.calls == 2
    assert second.token == "TOKEN-MARKER-2"
    assert second.device_id != first.device_id
    assert _DEVICE_ID.match(second.device_id)


@pytest.mark.asyncio
async def test_invalidating_a_stale_pair_does_not_start_a_new_acquisition() -> None:
    fetch = _Fetcher("TOKEN-MARKER-1", "TOKEN-MARKER-2", "TOKEN-MARKER-3")
    provider = _provider(fetch)
    stale = await _get_pair(provider)
    await provider.invalidate(stale)
    current = await _get_pair(provider)

    await provider.invalidate(stale)  # already replaced: no-op
    again = await _get_pair(provider)

    assert again is current
    assert fetch.calls == 2


@pytest.mark.asyncio
async def test_invalidating_with_nothing_cached_is_a_noop() -> None:
    fetch = _Fetcher()
    provider = _provider(fetch)

    await provider.invalidate(WbAccess("t", "d"))

    assert fetch.calls == 0
    assert (await _get_pair(provider)).token == "TOKEN-MARKER-1"


# --- AC4 (d): failure classification and pause --------------------------------


@pytest.mark.asyncio
async def test_fetch_error_with_a_stage_is_returned_as_that_stage_and_detail() -> None:
    fetch = _Fetcher(WbTokenFetchError(WbAccessStage.challenge_timeout, "d1"))

    result = await _get(_provider(fetch))

    assert result == WbAccessUnavailable(WbAccessStage.challenge_timeout, "d1")


@pytest.mark.asyncio
async def test_arbitrary_exception_is_launch_failed_with_class_name_only() -> None:
    """The message of the exception may carry anything (a URL, a token): only
    the class name reaches `detail`. Positive control: the marker is in the
    exception."""
    error = RuntimeError("secret-marker-in-message")
    fetch = _Fetcher(error)

    result = await _get(_provider(fetch))

    assert "secret-marker-in-message" in str(error)
    assert result == WbAccessUnavailable(WbAccessStage.launch_failed, "RuntimeError")


@pytest.mark.asyncio
async def test_bare_timeout_error_from_the_fetcher_is_launch_failed() -> None:
    """A `TimeoutError` raised by Playwright itself (launch or navigation
    timeout) while the provider's own deadline is far away is a failed launch."""
    fetch = _Fetcher(TimeoutError("playwright launch timeout"))

    result = await _get(_provider(fetch, token_timeout_seconds=30.0))

    assert result == WbAccessUnavailable(WbAccessStage.launch_failed, "TimeoutError")


@pytest.mark.asyncio
async def test_exceeding_the_deadline_is_challenge_timeout_and_cancels_the_fetch() -> (
    None
):
    fetch = _Fetcher(_HANG)

    result = await _get(_provider(fetch, token_timeout_seconds=0.01))

    assert result == WbAccessUnavailable(
        WbAccessStage.challenge_timeout, "no token within 0.01 s"
    )
    assert fetch.cancelled == 1  # the fetcher got to close its browser


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        WbTokenFetchError(WbAccessStage.launch_failed, "x"),
        RuntimeError("x"),
        TimeoutError(),
    ],
    ids=["stage-error", "arbitrary", "bare-timeout"],
)
async def test_a_failure_never_raises_out_of_get(plan: BaseException) -> None:
    result = await _get(_provider(_Fetcher(plan)))

    assert isinstance(result, WbAccessUnavailable)


@pytest.mark.asyncio
async def test_after_a_failure_get_is_paused_without_calling_the_fetcher() -> None:
    clock = _Clock()
    fetch = _Fetcher(RuntimeError("x"))
    provider = _provider(fetch, clock, failure_pause_seconds=300.0)
    await _get(provider)

    clock.now = 299.0
    during = await _get(provider)

    assert during == WbAccessUnavailable(WbAccessStage.paused, "pause after failure")
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_pause_ends_exactly_when_the_pause_has_elapsed() -> None:
    clock = _Clock()
    fetch = _Fetcher(RuntimeError("x"), "TOKEN-MARKER-2")
    provider = _provider(fetch, clock, failure_pause_seconds=300.0)
    await _get(provider)

    clock.now = 300.0
    after = await _get(provider)

    assert isinstance(after, WbAccess)
    assert after.token == "TOKEN-MARKER-2"
    assert fetch.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        WbTokenFetchError(WbAccessStage.challenge_timeout, "x"),
        WbTokenFetchError(WbAccessStage.launch_failed, "x"),
        TimeoutError(),
        RuntimeError("x"),
    ],
    ids=["challenge-error", "launch-error", "bare-timeout", "arbitrary"],
)
async def test_every_kind_of_failure_starts_the_pause(plan: BaseException) -> None:
    fetch = _Fetcher(plan)
    provider = _provider(fetch)
    await _get(provider)

    result = await _get(provider)

    assert isinstance(result, WbAccessUnavailable)
    assert result.stage is WbAccessStage.paused
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_a_deadline_failure_starts_the_pause_too() -> None:
    fetch = _Fetcher(_HANG)
    provider = _provider(fetch, token_timeout_seconds=0.01)
    await _get(provider)

    result = await _get(provider)

    assert isinstance(result, WbAccessUnavailable)
    assert result.stage is WbAccessStage.paused
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_concurrent_waiters_all_get_the_same_failure() -> None:
    gate = asyncio.Event()
    fetch = _Fetcher(RuntimeError("x"), gate=gate)
    provider = _provider(fetch)

    waiters = [asyncio.create_task(provider.get()) for _ in range(3)]
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*waiters), _FUSE_SECONDS)

    assert (
        results
        == [WbAccessUnavailable(WbAccessStage.launch_failed, "RuntimeError")] * 3
    )
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_a_success_after_a_failure_clears_the_pause() -> None:
    clock = _Clock()
    fetch = _Fetcher(RuntimeError("x"), "TOKEN-MARKER-2", "TOKEN-MARKER-3")
    provider = _provider(fetch, clock, failure_pause_seconds=300.0)
    await _get(provider)
    clock.now = 300.0
    pair = await _get_pair(provider)

    await provider.invalidate(pair)
    clock.now = 301.0
    third = await _get(provider)  # no pause after a success

    assert isinstance(third, WbAccess)
    assert third.token == "TOKEN-MARKER-3"


# --- AC4 (d)/(e): launch budget over a window ---------------------------------


async def _launch_and_drop(provider: BrowserWbAccessProvider) -> None:
    await provider.invalidate(await _get_pair(provider))


@pytest.mark.asyncio
async def test_budget_exhausted_blocks_the_next_launch_without_the_fetcher() -> None:
    clock = _Clock()
    fetch = _Fetcher()
    provider = _provider(fetch, clock, launch_budget=3, launch_window_seconds=100.0)
    for at in (0.0, 10.0, 20.0):
        clock.now = at
        await _launch_and_drop(provider)

    clock.now = 30.0
    result = await _get(provider)

    assert result == WbAccessUnavailable(
        WbAccessStage.budget_exhausted, "launch budget exhausted"
    )
    assert fetch.calls == 3


@pytest.mark.asyncio
async def test_budget_is_not_exhausted_one_launch_below_the_limit() -> None:
    clock = _Clock()
    fetch = _Fetcher()
    provider = _provider(fetch, clock, launch_budget=3, launch_window_seconds=100.0)
    for at in (0.0, 10.0):
        clock.now = at
        await _launch_and_drop(provider)

    clock.now = 20.0
    result = await _get(provider)

    assert isinstance(result, WbAccess)
    assert fetch.calls == 3


@pytest.mark.asyncio
async def test_launch_leaves_the_window_exactly_at_the_window_length() -> None:
    clock = _Clock()
    fetch = _Fetcher()
    provider = _provider(fetch, clock, launch_budget=3, launch_window_seconds=100.0)
    for at in (0.0, 10.0, 20.0):
        clock.now = at
        await _launch_and_drop(provider)

    clock.now = 99.0  # the oldest launch (0.0) is 99 s old: still inside
    inside = await _get(provider)
    clock.now = 100.0  # exactly 100 s old: outside
    outside = await _get(provider)

    assert isinstance(inside, WbAccessUnavailable)
    assert inside.stage is WbAccessStage.budget_exhausted
    assert isinstance(outside, WbAccess)
    assert fetch.calls == 4


@pytest.mark.asyncio
async def test_only_launches_that_left_the_window_are_forgotten() -> None:
    """After the oldest leaves, one slot is free, not all of them: with the
    launches at 0, 10, 20 and the clock at 100, a fourth launch fills the
    window again and the fifth is refused."""
    clock = _Clock()
    fetch = _Fetcher()
    provider = _provider(fetch, clock, launch_budget=3, launch_window_seconds=100.0)
    for at in (0.0, 10.0, 20.0):
        clock.now = at
        await _launch_and_drop(provider)
    clock.now = 100.0
    await _launch_and_drop(provider)

    clock.now = 101.0
    result = await _get(provider)

    assert isinstance(result, WbAccessUnavailable)
    assert result.stage is WbAccessStage.budget_exhausted
    assert fetch.calls == 4


@pytest.mark.asyncio
async def test_failed_launches_count_against_the_budget() -> None:
    """With no pause (0 s) failures follow each other; each is a launch."""
    clock = _Clock()
    fetch = _Fetcher(RuntimeError("x"))
    provider = _provider(
        fetch,
        clock,
        failure_pause_seconds=0.0,
        launch_budget=2,
        launch_window_seconds=100.0,
    )

    first = await _get(provider)
    second = await _get(provider)
    third = await _get(provider)

    assert isinstance(first, WbAccessUnavailable)
    assert first.stage is WbAccessStage.launch_failed
    assert isinstance(second, WbAccessUnavailable)
    assert second.stage is WbAccessStage.launch_failed
    assert isinstance(third, WbAccessUnavailable)
    assert third.stage is WbAccessStage.budget_exhausted
    assert fetch.calls == 2


@pytest.mark.asyncio
async def test_pause_is_reported_before_the_budget() -> None:
    clock = _Clock()
    fetch = _Fetcher(RuntimeError("x"))
    provider = _provider(fetch, clock, launch_budget=1, failure_pause_seconds=300.0)
    await _get(provider)

    result = await _get(provider)

    assert isinstance(result, WbAccessUnavailable)
    assert result.stage is WbAccessStage.paused


# --- AC4 (e): aclose ----------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_cancels_a_running_acquisition_and_waiters_get_closed() -> None:
    fetch = _Fetcher(_HANG)
    provider = _provider(fetch, token_timeout_seconds=30.0)
    waiters = [asyncio.create_task(provider.get()) for _ in range(3)]
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)

    await asyncio.wait_for(provider.aclose(), _FUSE_SECONDS)
    results = await asyncio.wait_for(asyncio.gather(*waiters), _FUSE_SECONDS)

    closed = WbAccessUnavailable(WbAccessStage.closed, "provider closed")
    assert results == [closed] * 3
    assert fetch.cancelled == 1  # the fetcher was cancelled and could clean up
    assert fetch.calls == 1


@pytest.mark.asyncio
async def test_get_after_aclose_is_closed_without_calling_the_fetcher() -> None:
    fetch = _Fetcher()
    provider = _provider(fetch)
    await provider.aclose()

    result = await _get(provider)

    assert result == WbAccessUnavailable(WbAccessStage.closed, "provider closed")
    assert fetch.calls == 0


@pytest.mark.asyncio
async def test_aclose_drops_the_cached_pair() -> None:
    provider = _provider(_Fetcher())
    await _get_pair(provider)

    await provider.aclose()

    result = await _get(provider)
    assert isinstance(result, WbAccessUnavailable)
    assert result.stage is WbAccessStage.closed


@pytest.mark.asyncio
async def test_aclose_does_not_leave_the_acquisition_task_running() -> None:
    fetch = _Fetcher(_HANG)
    provider = _provider(fetch)
    waiter = asyncio.create_task(provider.get())
    await asyncio.wait_for(fetch.started.wait(), _FUSE_SECONDS)
    before = {t for t in asyncio.all_tasks() if not t.done()}

    await asyncio.wait_for(provider.aclose(), _FUSE_SECONDS)
    await asyncio.wait_for(waiter, _FUSE_SECONDS)

    after = {t for t in asyncio.all_tasks() if not t.done()}
    assert len(before - after) >= 2  # the waiter and the acquisition are gone


@pytest.mark.asyncio
async def test_second_aclose_does_nothing() -> None:
    provider = _provider(_Fetcher())
    await provider.aclose()
    records, sink_id = _capture()
    try:
        await provider.aclose()
    finally:
        logger.remove(sink_id)

    assert records == []


# --- AC7: log levels and cardinality ------------------------------------------


@pytest.mark.asyncio
async def test_successful_acquisition_is_one_info_with_the_duration() -> None:
    clock = _Clock()
    fetch = _Fetcher(clock=clock, takes_seconds=2.5)
    records, sink_id = _capture()
    try:
        await _get_pair(_provider(fetch, clock))
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["INFO"]
    assert "2.5 s" in records[0]["message"]


@pytest.mark.asyncio
async def test_challenge_timeout_is_logged_as_one_warning() -> None:
    records, sink_id = _capture()
    try:
        await _get(_provider(_Fetcher(_HANG), token_timeout_seconds=0.01))
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["WARNING"]
    assert "challenge_timeout" in records[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        WbTokenFetchError(WbAccessStage.launch_failed, "x"),
        RuntimeError("x"),
        TimeoutError(),
    ],
    ids=["stage-error", "arbitrary", "bare-timeout"],
)
async def test_launch_failed_is_logged_as_one_error(plan: BaseException) -> None:
    records, sink_id = _capture()
    try:
        await _get(_provider(_Fetcher(plan)))
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["ERROR"]
    assert "launch_failed" in records[0]["message"]


@pytest.mark.asyncio
async def test_paused_is_logged_once_per_pause_not_per_get() -> None:
    clock = _Clock()
    fetch = _Fetcher(RuntimeError("x"))
    provider = _provider(fetch, clock, failure_pause_seconds=300.0)
    await _get(provider)
    records, sink_id = _capture()
    try:
        for _ in range(4):
            await _get(provider)
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["WARNING"]
    assert "paused" in records[0]["message"]


@pytest.mark.asyncio
async def test_a_new_pause_after_a_new_failure_is_logged_again() -> None:
    clock = _Clock()
    fetch = _Fetcher(RuntimeError("x"))
    provider = _provider(
        fetch,
        clock,
        failure_pause_seconds=300.0,
        launch_budget=10,
        launch_window_seconds=10_000,
    )
    await _get(provider)
    await _get(provider)  # first pause logged
    clock.now = 300.0
    await _get(provider)  # second failure
    records, sink_id = _capture()
    try:
        await _get(provider)  # second pause
        await _get(provider)
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["WARNING"]


@pytest.mark.asyncio
async def test_budget_exhausted_is_logged_as_one_error_not_per_get() -> None:
    clock = _Clock()
    provider = _provider(
        _Fetcher(), clock, launch_budget=1, launch_window_seconds=100.0
    )
    await _launch_and_drop(provider)
    records, sink_id = _capture()
    try:
        for _ in range(4):
            await _get(provider)
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["ERROR"]
    assert "budget" in records[0]["message"]


@pytest.mark.asyncio
async def test_budget_exhausted_is_logged_again_after_the_budget_recovered() -> None:
    clock = _Clock()
    provider = _provider(
        _Fetcher(), clock, launch_budget=1, launch_window_seconds=100.0
    )
    await _launch_and_drop(provider)
    await _get(provider)  # first budget record
    clock.now = 100.0
    await _launch_and_drop(provider)  # the budget recovered, then was spent again
    records, sink_id = _capture()
    try:
        await _get(provider)
        await _get(provider)
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["ERROR"]


@pytest.mark.asyncio
async def test_closed_is_logged_once_as_info_on_close_not_on_each_get() -> None:
    provider = _provider(_Fetcher())
    records, sink_id = _capture()
    try:
        await provider.aclose()
        await provider.aclose()
        for _ in range(3):
            await _get(provider)
    finally:
        logger.remove(sink_id)

    assert _levels(records) == ["INFO"]
    assert "closed" in records[0]["message"]


@pytest.mark.asyncio
async def test_token_and_deviceid_never_reach_any_log_record() -> None:
    """AC6: every path of the provider - success, failure with a secret in
    the exception message, pause, budget, close - logs no marker. Positive
    control: the token reached the pair the provider handed out and the
    exception message did contain the marker."""
    clock = _Clock()
    leaky = RuntimeError("TOKEN-MARKER-LEAK in the message")
    fetch = _Fetcher("TOKEN-MARKER-OK", leaky)
    provider = _provider(
        fetch,
        clock,
        failure_pause_seconds=10.0,
        launch_budget=2,
        launch_window_seconds=1000.0,
    )
    records, sink_id = _capture()
    try:
        pair = await _get_pair(provider)
        await provider.invalidate(pair)
        failure = await _get(provider)  # leaky failure
        paused = await _get(provider)
        clock.now = 10.0
        budget = await _get(provider)
        await provider.aclose()
    finally:
        logger.remove(sink_id)

    assert pair.token == "TOKEN-MARKER-OK"
    assert "TOKEN-MARKER-LEAK" in str(leaky)
    assert isinstance(failure, WbAccessUnavailable)
    assert isinstance(paused, WbAccessUnavailable)
    assert isinstance(budget, WbAccessUnavailable)
    assert {failure.stage, paused.stage, budget.stage} == {
        WbAccessStage.launch_failed,
        WbAccessStage.paused,
        WbAccessStage.budget_exhausted,
    }
    text = "".join(f"{r['message']}{r['extra']}" for r in records)
    text += failure.detail + paused.detail + budget.detail
    assert records
    assert "TOKEN-MARKER" not in text
    assert pair.device_id not in text
