"""Interactive region picker used by the setup wizard (design doc 5.2, FR-11).

Reached through the :class:`RegionPicker` protocol so tests never open a real
window. Production wires in :class:`TkDragPicker`, a fullscreen semi-transparent
tkinter overlay that spans every monitor; the user drags a rectangle and it is
returned in absolute virtual-desktop coordinates. Tests wire in
:class:`ScriptedPicker`, which returns a pre-set region.
"""

from __future__ import annotations

from typing import Any, Protocol

Region = dict[str, int]


class PickerError(RuntimeError):
    """The picker cannot run on this machine at all (missing tkinter, missing display)."""


class RegionPicker(Protocol):
    """Return the picked region, or ``None`` when the user dismissed the picker."""

    def pick(self) -> Region | None: ...


class ScriptedPicker:
    """Test double. Returns the pre-set region (or ``None`` to simulate cancel)."""

    def __init__(self, region: Region | None) -> None:
        self._region = region

    def pick(self) -> Region | None:
        return self._region


class TkDragPicker:
    """Fullscreen semi-transparent overlay across the virtual desktop.

    Mouse-down + drag + release returns the picked rectangle. Esc, closing the
    window, or a zero-area click all return ``None`` — the caller decides what
    that means (the wizard treats it as "use a default centered region").
    """

    def pick(self) -> Region | None:
        try:
            import tkinter as tk
        except ImportError:
            raise PickerError(
                "tkinter is not available on this Python. Install a full Python "
                "distribution to use the interactive region picker."
            ) from None
        try:
            import mss
        except ImportError as exc:
            raise PickerError(
                f"Missing capture dependency ({exc.name}). "
                "Install with: pip install screenrecon"
            ) from None

        # sct.monitors[0] is the union of all monitors — one overlay covers them all.
        factory = getattr(mss, "MSS", None) or mss.mss  # mss 11 drops the lowercase form
        with factory() as sct:
            virtual = sct.monitors[0]

        root = tk.Tk()
        root.overrideredirect(True)
        root.geometry(
            f"{virtual['width']}x{virtual['height']}"
            f"+{virtual['left']}+{virtual['top']}"
        )
        root.attributes("-alpha", 0.30)
        root.configure(bg="black")
        root.attributes("-topmost", True)
        root.lift()
        # Keyboard focus so <Escape> fires without a click first.
        root.focus_force()

        canvas = tk.Canvas(root, cursor="cross", bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)

        # Mutable state shared with event handlers.
        state: dict[str, Any] = {"start": None, "rect": None, "result": None}

        def _to_canvas(x_root: int, y_root: int) -> tuple[int, int]:
            return x_root - virtual["left"], y_root - virtual["top"]

        def on_press(event: Any) -> None:
            state["start"] = (event.x_root, event.y_root)
            cx, cy = _to_canvas(event.x_root, event.y_root)
            state["rect"] = canvas.create_rectangle(
                cx, cy, cx, cy, outline="red", width=2
            )

        def on_drag(event: Any) -> None:
            if state["start"] is None or state["rect"] is None:
                return
            sx, sy = state["start"]
            cx0, cy0 = _to_canvas(sx, sy)
            cx1, cy1 = _to_canvas(event.x_root, event.y_root)
            canvas.coords(state["rect"], cx0, cy0, cx1, cy1)

        def on_release(event: Any) -> None:
            if state["start"] is None:
                return
            sx, sy = state["start"]
            ex, ey = event.x_root, event.y_root
            width = abs(ex - sx)
            height = abs(ey - sy)
            # A zero-area click is treated as cancel (a stray click, not a region).
            if width == 0 or height == 0:
                state["result"] = None
            else:
                state["result"] = {
                    "left": int(min(sx, ex)),
                    "top": int(min(sy, ey)),
                    "width": int(width),
                    "height": int(height),
                }
            root.destroy()

        def on_cancel(_event: Any = None) -> None:
            state["result"] = None
            root.destroy()

        canvas.bind("<Button-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        root.bind("<Escape>", on_cancel)
        root.protocol("WM_DELETE_WINDOW", on_cancel)

        try:
            root.mainloop()
        except Exception as exc:
            raise PickerError(
                f"Picker failed: {type(exc).__name__}: {exc}"
            ) from exc

        return state["result"]


def default_picker() -> RegionPicker:
    """Return the production picker (:class:`TkDragPicker`)."""
    return TkDragPicker()


DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480


def default_region_at(x: int, y: int) -> Region:
    """Return a ``DEFAULT_WIDTH x DEFAULT_HEIGHT`` region centred on the monitor
    that contains ``(x, y)``. Falls back to the first monitor when the cursor is
    off-screen, or to the point itself when no monitors are known.

    Used by the wizard when the user cancels the picker: we still need *some*
    region to save, and centering on the monitor the user is looking at is the
    least-surprising choice.
    """
    from . import platform as plat

    monitors = plat.enumerate_monitors()
    if not monitors:
        return {"left": x, "top": y, "width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT}
    mon = plat.find_monitor_containing(x, y) or monitors[0]
    return {
        "left": mon["left"] + (mon["width"] - DEFAULT_WIDTH) // 2,
        "top": mon["top"] + (mon["height"] - DEFAULT_HEIGHT) // 2,
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
    }
