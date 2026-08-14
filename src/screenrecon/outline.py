"""Debug border overlay around the watched region (``--debug`` flag).

Four thin borderless always-on-top Tk windows form the top / bottom / left /
right edges of a rectangle sitting just outside the capture region. The
region's interior has no window over it, so clicks pass through naturally
(nothing to be transparent to). This avoids platform-specific click-through
APIs — ``-transparentcolor`` is Windows-only, ``WS_EX_TRANSPARENT`` /
``setIgnoresMouseEvents:`` / XShape all differ per platform — and works
under plain Tk on Windows, macOS, and X11 Linux.

The border sits *outside* the region rather than inside or straddling it, so
it can never appear in a capture. The narrow border strips themselves do
block clicks in those few pixels; that is an acceptable trade for a debug
overlay.

Best-effort by design: if Tk is unavailable (missing tkinter, headless, or
Tk raises during setup) the overlay simply does not open — a warning is
printed and the watcher continues normally.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import ui

BORDER_THICKNESS = 3
BORDER_COLOR = "red"


def edge_rects(region: Mapping[str, Any]) -> list[tuple[int, int, int, int]]:
    """Return the four ``(x, y, w, h)`` rectangles for the border strips.

    Order: top, bottom, left, right. Each strip sits just outside the region;
    the corners are covered by the horizontal (top / bottom) strips so the
    border reads as a closed rectangle.
    """
    left = int(region["left"])
    top = int(region["top"])
    width = int(region["width"])
    height = int(region["height"])
    b = BORDER_THICKNESS
    return [
        (left - b, top - b, width + 2 * b, b),        # top (spans corners)
        (left - b, top + height, width + 2 * b, b),   # bottom (spans corners)
        (left - b, top, b, height),                   # left
        (left + width, top, b, height),               # right
    ]


class RegionOutline:
    """Persistent border overlay. Use as a context manager or open/close pair.

    ``poll()`` pumps pending Tk events (WM redraw / move requests) and should
    be called from the watcher loop so the overlay survives desktop changes.
    """

    def __init__(self, region: Mapping[str, Any]) -> None:
        self._region = region
        self._root: Any = None
        self._windows: list[Any] = []

    def open(self) -> None:
        try:
            import tkinter as tk
        except ImportError:
            ui.warn("Debug outline unavailable: tkinter is not installed.")
            return

        try:
            self._root = tk.Tk()
            # The Tk root is a real window we do not want to see; hide it and
            # let the four Toplevel edges do all the drawing.
            self._root.withdraw()
            for x, y, w, h in edge_rects(self._region):
                win = tk.Toplevel(self._root)
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                win.configure(bg=BORDER_COLOR)
                win.geometry(f"{w}x{h}+{x}+{y}")
                self._windows.append(win)
            # Flush window-creation events so the strips paint immediately.
            self._root.update()
        except Exception as exc:
            ui.warn(f"Debug outline unavailable: {exc}")
            self.close()

    def poll(self) -> None:
        """Process pending Tk events. Cheap; safe to call every poll tick."""
        if self._root is None:
            return
        try:
            self._root.update()
        except Exception:
            # A destroyed root or a WM disconnect must not take the watcher
            # down — the overlay is a debug aid, not a hard dependency.
            pass

    def close(self) -> None:
        for win in self._windows:
            try:
                win.destroy()
            except Exception:
                pass
        self._windows = []
        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None

    def __enter__(self) -> RegionOutline:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
