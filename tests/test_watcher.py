"""Trigger state machine tests (design doc 8.1).

The full sequence: enter -> dwell -> fire -> stay parked without re-firing ->
leave -> re-arm -> fire again. Clock and cursor are both faked.
"""

from __future__ import annotations

import pytest

from screenrecon.watcher import DwellTrigger

REGION = {"left": 100, "top": 100, "width": 200, "height": 100}


def make_trigger(dwell: float = 3.0) -> DwellTrigger:
    return DwellTrigger(REGION, dwell)


class FakeClock:
    """Monotonic clock we control tick by tick.

    Elapsed time is tracked in whole milliseconds so that repeated 0.1s ticks do
    not accumulate floating-point drift across a long test sequence.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.start = start
        self.elapsed_ms = 0

    @property
    def now(self) -> float:
        return self.start + self.elapsed_ms / 1000.0

    def advance(self, seconds: float) -> float:
        self.elapsed_ms += round(seconds * 1000)
        return self.now


def drive(trigger: DwellTrigger, position, clock: FakeClock, seconds: float, step: float = 0.1):
    """Hold the cursor at ``position`` for ``seconds``; return the number of fires."""
    fires = 0
    for _ in range(round(seconds / step)):
        clock.advance(step)
        if trigger.update(position, clock.now):
            fires += 1
    return fires


# --------------------------------------------------------------------------- #
# Region containment (half-open interval)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ((100, 100), True),  # top-left corner is inclusive
        ((299, 199), True),  # bottom-right inner edge is inclusive
        ((300, 150), False),  # left + width is exclusive
        ((150, 200), False),  # top + height is exclusive
        ((99, 150), False),
        ((150, 99), False),
        ((0, 0), False),
    ],
)
def test_contains_uses_half_open_interval(position, expected):
    assert make_trigger().contains(position) is expected


def test_contains_supports_negative_origin():
    """Secondary monitors can live at negative coordinates."""
    trigger = DwellTrigger({"left": -1920, "top": -100, "width": 200, "height": 100}, 3)
    assert trigger.contains((-1900, -50)) is True
    assert trigger.contains((-1921, -50)) is False


# --------------------------------------------------------------------------- #
# Full trigger sequence
# --------------------------------------------------------------------------- #


def test_full_sequence_enter_dwell_fire_park_leave_rearm():
    clock = FakeClock()
    trigger = make_trigger(dwell=3.0)
    inside = (150, 150)
    outside = (10, 10)

    # Outside: never fires, stays armed.
    assert drive(trigger, outside, clock, seconds=1.0) == 0
    assert trigger.armed is True

    # Enter and dwell: exactly one fire once the dwell threshold is crossed.
    assert drive(trigger, inside, clock, seconds=3.5) == 1
    assert trigger.armed is False

    # Parked in place for a long time: no repeat (FR-10).
    assert drive(trigger, inside, clock, seconds=60.0) == 0

    # Leave: re-arms immediately.
    assert drive(trigger, outside, clock, seconds=0.5) == 0
    assert trigger.armed is True

    # Re-enter and dwell: fires again.
    assert drive(trigger, inside, clock, seconds=3.5) == 1


def test_dwell_timer_resets_when_leaving_early():
    clock = FakeClock()
    trigger = make_trigger(dwell=3.0)

    assert drive(trigger, (150, 150), clock, seconds=2.0) == 0  # not long enough
    assert drive(trigger, (10, 10), clock, seconds=0.2) == 0  # leaves, timer resets
    assert trigger.entered_at is None
    assert drive(trigger, (150, 150), clock, seconds=2.0) == 0  # restarts from zero
    assert drive(trigger, (150, 150), clock, seconds=1.2) == 1  # now past the threshold


def test_fires_at_the_threshold_not_before():
    clock = FakeClock()
    trigger = make_trigger(dwell=1.0)
    inside = (150, 150)

    trigger.update(inside, clock.now)  # first tick records entry
    assert trigger.update(inside, clock.now + 0.99) is False
    assert trigger.update(inside, clock.now + 1.0) is True


def test_moving_within_region_does_not_reset_the_timer():
    """Only leaving the region resets the dwell timer, not movement inside it."""
    clock = FakeClock()
    trigger = make_trigger(dwell=2.0)
    fires = 0

    trigger.update((110, 110), clock.advance(0.1))  # entry
    for offset in range(20):  # keep moving inside the region for 2.0s
        if trigger.update((120 + offset, 130), clock.advance(0.1)):
            fires += 1
    assert fires == 1


def test_dwelled_for_reports_progress():
    clock = FakeClock()
    trigger = make_trigger(dwell=5.0)

    assert trigger.dwelled_for(clock.now) == 0.0
    trigger.update((150, 150), clock.now)
    assert trigger.dwelled_for(clock.advance(2.0)) == pytest.approx(2.0)
    trigger.update((10, 10), clock.now)
    assert trigger.dwelled_for(clock.now) == 0.0


def test_fractional_dwell_seconds():
    clock = FakeClock()
    trigger = make_trigger(dwell=0.25)
    assert drive(trigger, (150, 150), clock, seconds=0.4) == 1
