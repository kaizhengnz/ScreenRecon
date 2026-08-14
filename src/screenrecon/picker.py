"""Interactive region picker used by the setup wizard (design doc 5.9).

Reached through the :class:`RegionPicker` protocol so tests never open a real
window. Production wires in :class:`TkDragPicker` (a fullscreen semi-transparent
tkinter overlay that spans every monitor); tests wire in :class:`ScriptedPicker`,
which returns a pre-set region.

The wizard calls one entry point — :func:`pick_region_or_default` — which owns
the whole "give me a region, one way or another" concern: open the picker, fall
back to a centred default on cancel, keep the current region if either the
picker or the cursor cannot be read.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

Region = dict[str, int]
PickerFactory = Callable[[], "RegionPicker"]


class PickerError(RuntimeError):
    """The picker cannot run on this machine at all (missing tkinter, missing display)."""


class RegionPicker(Protocol):
    """Return the picked region, or ``None`` if the user dismissed the picker.

    ``None`` is deliberately overloaded: Esc, window closed, and a zero-area
    click all collapse to the same "no region was chosen" signal because the
    wizard treats them identically. If a future picker needs to distinguish
    these outcomes, replace the union with a small result type here — every
    call site already only cares whether the return is truthy.
    """

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
    window, or a zero-area click all return ``None`` — the wizard treats those
    as "use a default centered region".
    """

    def pick(self) -> Region | None:
        try:
            import tkinter as tk
        except ImportError:
            raise PickerError(
                "tkinter is not available on this Python. Install a full Python "
                "distribution to use the interactive region picker."
            ) from None

        from . import display

        try:
            virtual = display.virtual_desktop_bounds()
        except ImportError as exc:
            raise PickerError(
                f"Missing capture dependency ({exc.name}). "
                "Install with: pip install screenrecon"
            ) from None

        root = tk.Tk()
        state: dict[str, Any] = {
            "start": None,
            "rect": None,
            "result": None,
            "callback_error": None,
            "notice": None,
        }

        try:
            root.overrideredirect(True)
            root.geometry(
                f"{virtual['width']}x{virtual['height']}"
                f"+{virtual['left']}+{virtual['top']}"
            )
            root.attributes("-alpha", 0.30)
            root.configure(bg="black")
            root.attributes("-topmost", True)
            root.lift()
            root.focus_force()  # so <Escape> fires without a first click

            # Tk normally swallows callback exceptions and prints a traceback; stash
            # the first one so we can re-raise as PickerError after mainloop exits.
            def _on_callback_exception(exc_type, exc_value, _tb):
                if state["callback_error"] is None:
                    state["callback_error"] = (exc_type, exc_value)
                root.after_idle(root.destroy)

            root.report_callback_exception = _on_callback_exception

            canvas = tk.Canvas(root, cursor="cross", bg="black", highlightthickness=0)
            canvas.pack(fill="both", expand=True)

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
                if width == 0 or height == 0:
                    state["result"] = None  # stray click, not a region
                    state["notice"] = "   Zero-area selection, treating as cancel."
                else:
                    state["result"] = {
                        "left": int(min(sx, ex)),
                        "top": int(min(sy, ey)),
                        "width": int(width),
                        "height": int(height),
                    }
                # Deferred destroy: tearing down the toplevel from inside a mouse
                # handler while -topmost + overrideredirect are set has historically
                # caused focus lockups on macOS.
                root.after_idle(root.destroy)

            def on_cancel(_event: Any = None) -> None:
                state["result"] = None
                root.after_idle(root.destroy)

            canvas.bind("<Button-1>", on_press)
            canvas.bind("<B1-Motion>", on_drag)
            canvas.bind("<ButtonRelease-1>", on_release)
            root.bind("<Escape>", on_cancel)
            root.protocol("WM_DELETE_WINDOW", on_cancel)

            root.mainloop()
        finally:
            # mainloop calls root.destroy itself in every path we know, but a bug
            # or a caught callback exception can bypass that. Belt-and-braces so
            # the next TkDragPicker on this thread does not hit "main thread is
            # not in main loop".
            try:
                root.destroy()
            except Exception:
                pass

        if state["callback_error"] is not None:
            exc_type, exc_value = state["callback_error"]
            raise PickerError(f"Picker failed: {exc_type.__name__}: {exc_value}")
        if state["notice"] is not None:
            from . import ui
            ui.info(state["notice"])
        return state["result"]


def default_picker() -> RegionPicker:
    """Return the production picker for this platform.

    Only :class:`TkDragPicker` exists today (covers Windows, macOS, X11 Linux).
    When a Wayland-native or headless picker is added, dispatch here on
    ``sys.platform`` the same way :func:`platform._build_reader` does — do not
    grow ``if/elif`` chains at call sites.
    """
    return TkDragPicker()


def default_region_at(x: int, y: int) -> Region:
    """Return a ``DEFAULT_WIDTH x DEFAULT_HEIGHT`` region centred on the monitor
    that contains ``(x, y)``. Falls back to the first monitor when the point
    lies outside every known monitor (e.g. a stale cursor position after a
    monitor was unplugged, or a coordinate on a virtual layout where the
    primary display starts at negative pixels).
    """
    from . import display

    monitors = display.enumerate_monitors()
    if not monitors:
        return {"left": x, "top": y, "width": DEFAULT_WIDTH, "height": DEFAULT_HEIGHT}
    mon = display.find_monitor_containing(x, y, monitors) or monitors[0]
    return {
        "left": mon["left"] + (mon["width"] - DEFAULT_WIDTH) // 2,
        "top": mon["top"] + (mon["height"] - DEFAULT_HEIGHT) // 2,
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
    }


def default_centered_region_or_none() -> Region | None:
    """A ``DEFAULT_WIDTH x DEFAULT_HEIGHT`` region centred on the cursor's
    monitor, or ``None`` if the cursor cannot be read (locked screen, missing
    X display, missing dependency). Callers pick the fallback that fits.
    """
    from . import platform as cursor_platform

    try:
        cursor_platform.ensure_reader()
        x, y = cursor_platform.get_cursor_pos()
    except (cursor_platform.CursorError, cursor_platform.CursorUnavailable):
        return None
    return default_region_at(x, y)


def pick_region_or_default(
    current: Region, factory: PickerFactory | None = None
) -> Region:
    """Open the picker and return a region — the wizard's one entry point.

    Behaviour:

    - Picker returns a region → return it (and log which monitor it landed on).
    - Picker returns ``None`` (Esc / close / zero-area click) → return a
      centred default if the cursor is readable, else keep ``current``.
    - Picker raises :class:`PickerError` (missing tkinter, no display) → warn
      and keep ``current``.
    """
    from . import display, ui

    factory = factory or default_picker
    ui.info(
        "   Opening the picker. Drag a rectangle, or press Esc to use "
        f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT} centered on the current monitor."
    )
    try:
        picked = factory().pick()
    except PickerError as exc:
        ui.warn(f"   {exc}")
        ui.info("   Keeping the current region.")
        return current

    if picked is not None:
        _report_picked_region(picked, display.enumerate_monitors())
        return picked

    default = default_centered_region_or_none()
    if default is None:
        ui.warn("   Could not read cursor position for the default region.")
        ui.info("   Keeping the current region.")
        return current
    ui.info(
        f"   Using default: left={default['left']} top={default['top']} "
        f"width={default['width']} height={default['height']}"
    )
    return default


def _report_picked_region(region: Region, monitors: list[Region]) -> None:
    """Print 'Picked: ... (on monitor N of M)' to the UI."""
    from . import display, ui

    ui.info(
        f"   Picked: left={region['left']} top={region['top']} "
        f"width={region['width']} height={region['height']}"
        + display.describe_region_monitor(region, monitors)
    )
