"""Region picker seam (design doc 5.2, FR-11).

The real :class:`TkDragPicker` opens a window and is exempt from the
"no screen in tests" rule (NFR-6); the wizard interacts with it through the
:class:`RegionPicker` protocol so :class:`ScriptedPicker` covers every branch
these tests need.
"""

from __future__ import annotations

import pytest

from screenrecon import picker

# --------------------------------------------------------------------------- #
# ScriptedPicker
# --------------------------------------------------------------------------- #


def test_scripted_picker_returns_the_given_region():
    region = {"left": 10, "top": 20, "width": 300, "height": 200}
    assert picker.ScriptedPicker(region).pick() == region


def test_scripted_picker_returns_none_on_cancel():
    assert picker.ScriptedPicker(None).pick() is None


# --------------------------------------------------------------------------- #
# default_region_at (the fallback the wizard applies on Esc)
# --------------------------------------------------------------------------- #


def test_default_region_centers_on_the_monitor_containing_the_cursor(monkeypatch):
    monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 2560, "height": 1440},
    ]
    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: monitors)
    monkeypatch.setattr(
        "screenrecon.display.find_monitor_containing",
        lambda x, y, mons=None: monitors[1] if x >= 1920 else monitors[0],
    )

    # Cursor is somewhere on monitor 2 (the 2560x1440 one).
    region = picker.default_region_at(3000, 700)
    assert region["width"] == picker.DEFAULT_WIDTH
    assert region["height"] == picker.DEFAULT_HEIGHT
    # Centered on monitor 2: left = 1920 + (2560-640)/2 = 1920 + 960 = 2880
    assert region["left"] == 1920 + (2560 - picker.DEFAULT_WIDTH) // 2
    assert region["top"] == 0 + (1440 - picker.DEFAULT_HEIGHT) // 2


def test_default_region_falls_back_to_first_monitor_when_cursor_is_off_screen(monkeypatch):
    monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: monitors)
    monkeypatch.setattr(
        "screenrecon.display.find_monitor_containing", lambda x, y, mons=None: None
    )

    region = picker.default_region_at(-500, -500)  # off-screen
    assert region["left"] == (1920 - picker.DEFAULT_WIDTH) // 2
    assert region["top"] == (1080 - picker.DEFAULT_HEIGHT) // 2


def test_describe_region_monitor_names_the_containing_monitor(monkeypatch):
    from screenrecon import display

    monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 1600, "height": 900},
    ]
    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: monitors)
    # Region centred on the second monitor.
    result = display.describe_region_monitor(
        {"left": 2200, "top": 200, "width": 400, "height": 300}
    )
    assert result == " (on monitor 2 of 2)"


def test_describe_region_monitor_falls_back_to_nearest_monitor(monkeypatch):
    """A region whose centre lies on no monitor still gets an annotation —
    the nearest monitor by squared distance to its rectangle. Silence would
    leave the user staring at a coordinate line with no idea which display
    the config is aimed at (the situation stale-region and pre-SR-20 DPI
    configs land in)."""
    from screenrecon import display

    monitors = [
        {"left": 0, "top": 0, "width": 3456, "height": 2168},
        {"left": 1728, "top": -1440, "width": 2560, "height": 1440},
    ]
    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: monitors)
    # Region centre at (-1352, 745) — off both monitors, but monitor 1 (whose
    # left edge is at x=0) is closer than monitor 2 (left edge at x=1728).
    assert (
        display.describe_region_monitor(
            {"left": -1776, "top": 472, "width": 848, "height": 547}
        )
        == " (on monitor 1 of 2)"
    )


def test_describe_region_monitor_is_empty_when_no_monitors_are_known(monkeypatch):
    """Headless CI / mss failure: no monitors known, so nothing to say."""
    from screenrecon import display

    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: [])
    assert (
        display.describe_region_monitor(
            {"left": 0, "top": 0, "width": 10, "height": 10}
        )
        == ""
    )


def test_describe_region_monitor_is_empty_when_region_is_incomplete(monkeypatch):
    """A region missing keys must not raise — the annotation is skipped instead."""
    from screenrecon import display

    monkeypatch.setattr(
        "screenrecon.display.enumerate_monitors",
        lambda: [{"left": 0, "top": 0, "width": 100, "height": 100}],
    )
    assert display.describe_region_monitor({"left": 5}) == ""


def test_default_region_falls_back_to_cursor_when_no_monitors_are_known(monkeypatch):
    """Extremely defensive — headless CI or a mss quirk. Region still valid."""
    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: [])
    monkeypatch.setattr(
        "screenrecon.display.find_monitor_containing", lambda x, y, mons=None: None
    )

    region = picker.default_region_at(42, 99)
    assert region == {
        "left": 42,
        "top": 99,
        "width": picker.DEFAULT_WIDTH,
        "height": picker.DEFAULT_HEIGHT,
    }


# --------------------------------------------------------------------------- #
# default_picker (production factory)
# --------------------------------------------------------------------------- #


def test_default_picker_returns_the_tk_dragger():
    """The wizard's default path must land on the real picker, not a stub."""
    assert isinstance(picker.default_picker(), picker.TkDragPicker)


def test_tk_dragger_reports_picker_error_when_tkinter_is_missing(monkeypatch):
    """A minimal Python without _tkinter must fail with a message the user can act on."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tkinter":
            raise ImportError("no _tkinter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(picker.PickerError, match="tkinter"):
        picker.TkDragPicker().pick()
