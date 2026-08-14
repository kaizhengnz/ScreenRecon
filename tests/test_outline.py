"""Debug outline overlay (--debug flag).

The four edge windows are Tk toplevels and cannot be opened in the test
suite (NFR-6 forbids touching the screen). What is covered here:

- The geometry helper — the only piece that has real logic worth pinning.
- The graceful failure paths (tkinter missing, Tk construction blows up),
  so ``--debug`` on a headless or misconfigured environment never crashes
  the watcher.
- ``close()`` is idempotent and safe to call before ``open()``.
"""

from __future__ import annotations

import builtins

import pytest

from screenrecon import outline

# --------------------------------------------------------------------------- #
# edge_rects
# --------------------------------------------------------------------------- #


def test_edge_rects_frame_sits_just_outside_the_region():
    region = {"left": 100, "top": 50, "width": 400, "height": 300}
    b = outline.BORDER_THICKNESS
    top, bottom, left, right = outline.edge_rects(region)

    # Horizontal strips span the full width plus both corners.
    assert top == (100 - b, 50 - b, 400 + 2 * b, b)
    assert bottom == (100 - b, 50 + 300, 400 + 2 * b, b)
    # Vertical strips sit between the horizontal ones (no corner overlap).
    assert left == (100 - b, 50, b, 300)
    assert right == (100 + 400, 50, b, 300)


def test_edge_rects_never_overlap_the_capture_area():
    """The border must sit outside the region so it cannot appear in captures."""
    region = {"left": 200, "top": 100, "width": 500, "height": 400}
    cap_right = region["left"] + region["width"]
    cap_bottom = region["top"] + region["height"]

    for x, y, w, h in outline.edge_rects(region):
        strip_right = x + w
        strip_bottom = y + h
        # No pixel of any strip may sit inside [left, right) x [top, bottom).
        outside = (
            strip_right <= region["left"]
            or x >= cap_right
            or strip_bottom <= region["top"]
            or y >= cap_bottom
        )
        assert outside, f"strip {(x, y, w, h)} intrudes into capture region"


def test_edge_rects_handles_negative_origin():
    """Secondary monitors sit at negative coordinates; the helper must not choke."""
    region = {"left": -1920, "top": -100, "width": 800, "height": 600}
    b = outline.BORDER_THICKNESS
    top, _bottom, left, right = outline.edge_rects(region)
    assert top[0] == -1920 - b
    assert left[0] == -1920 - b
    assert right[0] == -1920 + 800


# --------------------------------------------------------------------------- #
# Graceful failure paths
# --------------------------------------------------------------------------- #


def test_open_is_a_silent_noop_when_tkinter_is_missing(monkeypatch, capsys):
    """A minimal Python without tkinter must not crash the watcher."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tkinter":
            raise ImportError("no _tkinter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    overlay = outline.RegionOutline({"left": 0, "top": 0, "width": 10, "height": 10})
    overlay.open()  # must not raise
    overlay.poll()  # must not raise even though open() bailed
    overlay.close()  # must not raise
    # ui.warn writes to stdout (see ui.py); combine both streams to be robust.
    captured = capsys.readouterr()
    assert "tkinter" in (captured.out + captured.err)


def test_open_warns_and_recovers_when_tk_construction_fails(monkeypatch, capsys):
    """Headless CI / no display: Tk() raises; the watcher must keep going."""
    import tkinter as tk

    def fake_tk(*args, **kwargs):
        raise tk.TclError("no display name and no $DISPLAY environment variable")

    monkeypatch.setattr(tk, "Tk", fake_tk)
    overlay = outline.RegionOutline({"left": 0, "top": 0, "width": 10, "height": 10})
    overlay.open()  # must not raise
    captured = capsys.readouterr()
    assert "Debug outline unavailable" in (captured.out + captured.err)
    # Post-condition: overlay is in the closed state, so poll/close are safe.
    overlay.poll()
    overlay.close()


def test_close_before_open_is_safe():
    overlay = outline.RegionOutline({"left": 0, "top": 0, "width": 10, "height": 10})
    overlay.close()  # no-op, must not raise


def test_context_manager_calls_open_and_close(monkeypatch):
    calls: list[str] = []
    overlay = outline.RegionOutline({"left": 0, "top": 0, "width": 10, "height": 10})
    monkeypatch.setattr(overlay, "open", lambda: calls.append("open"))
    monkeypatch.setattr(overlay, "close", lambda: calls.append("close"))
    with overlay as returned:
        assert returned is overlay
    assert calls == ["open", "close"]


# --------------------------------------------------------------------------- #
# ImportError check for tkinter is via a fake import above; skip the real Tk
# path when tkinter is absent so this file is still importable.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _require_tkinter_for_construction_test(request):
    """Skip the Tk-construction test on interpreters missing tkinter."""
    if request.node.name != "test_open_warns_and_recovers_when_tk_construction_fails":
        return
    pytest.importorskip("tkinter")
