"""Unit tests for ``RefreshLimiter`` (PAB-100 AC1).

The clock is an injected callable, so every instant is a literal and nothing
here depends on real time. ``reserve`` is synchronous: the tests need no event
loop, which is also the property that makes it atomic in production.
"""

from app.services.price_service import RefreshLimiter
from app.services.price_service import RefreshThrottled


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _limiter(clock: _Clock, window: float = 60.0) -> RefreshLimiter:
    return RefreshLimiter(window, clock)


def test_first_reserve_of_a_key_is_accepted() -> None:
    assert _limiter(_Clock()).reserve(1) is None


def test_repeat_in_the_same_instant_waits_the_whole_window() -> None:
    limiter = _limiter(_Clock())
    limiter.reserve(1)

    assert limiter.reserve(1) == RefreshThrottled(retry_after_seconds=60)


def test_repeat_half_a_second_before_the_window_ends_waits_one_second() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    limiter.reserve(1)
    clock.now += 59.5

    assert limiter.reserve(1) == RefreshThrottled(retry_after_seconds=1)


def test_remaining_time_is_rounded_up_not_down() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    limiter.reserve(1)
    clock.now += 30.2  # 29.8 s left

    assert limiter.reserve(1) == RefreshThrottled(retry_after_seconds=30)


def test_repeat_exactly_at_the_window_end_is_accepted() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    limiter.reserve(1)
    clock.now += 60.0

    assert limiter.reserve(1) is None


def test_repeat_just_before_the_window_end_is_refused() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    limiter.reserve(1)
    clock.now += 59.999

    assert isinstance(limiter.reserve(1), RefreshThrottled)


def test_a_refused_attempt_does_not_move_the_window() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    limiter.reserve(1)
    clock.now += 30.0
    assert isinstance(limiter.reserve(1), RefreshThrottled)
    clock.now += 30.0  # 60 s after the accepted mark, 30 s after the refused one

    assert limiter.reserve(1) is None


def test_the_window_runs_from_the_latest_accepted_mark() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    limiter.reserve(1)
    clock.now += 60.0
    assert limiter.reserve(1) is None
    clock.now += 10.0

    assert limiter.reserve(1) == RefreshThrottled(retry_after_seconds=50)


def test_keys_are_independent() -> None:
    limiter = _limiter(_Clock())
    limiter.reserve(1)

    assert limiter.reserve(2) is None
    assert isinstance(limiter.reserve(1), RefreshThrottled)


def test_a_refused_key_adds_no_entry() -> None:
    limiter = _limiter(_Clock())
    limiter.reserve(1)
    limiter.reserve(1)
    limiter.reserve(1)

    assert len(limiter) == 1


def test_a_thousand_keys_are_purged_after_the_window_leaving_only_the_new_one() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    for key in range(1_000):
        assert limiter.reserve(key) is None
    assert len(limiter) == 1_000
    clock.now += 61.0

    assert limiter.reserve(5_000) is None
    assert len(limiter) == 1


def test_a_purged_key_is_accepted_again() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    limiter.reserve(7)
    clock.now += 61.0
    limiter.reserve(8)  # purges key 7

    assert limiter.reserve(7) is None


def test_the_window_comes_from_the_constructor() -> None:
    clock = _Clock()
    limiter = _limiter(clock, window=5.0)
    limiter.reserve(1)
    clock.now += 4.0

    assert limiter.reserve(1) == RefreshThrottled(retry_after_seconds=1)
    clock.now += 1.0
    assert limiter.reserve(1) is None
