"""Cross-platform cursor position (design doc 5.2).

Exposes ``get_cursor_pos() -> tuple[int, int]``. The implementation is chosen by
``sys.platform`` and bound lazily on first use, so ``--help`` still works when an
optional platform dependency is missing.

Two failure kinds are deliberately distinguished:

* :class:`CursorError` — this machine cannot support the tool at all (unsupported
  platform, missing dependency, no X display). Fatal.
* :class:`CursorUnavailable` — the cursor cannot be read *right now*. On Windows
  this happens whenever the process is not attached to the input desktop: the
  workstation is locked, a UAC prompt is up, or an RDP session is disconnected.
  A watcher meant to run all day must wait these out, not exit.

Coordinate-system consistency: on Windows, mss marks the process DPI-aware, after
which GetCursorPos reports physical pixels. This module declares DPI awareness
first so that the setup wizard's region picker and the watch loop always agree,
whichever runs.

Note: this module shadows the standard library's ``platform`` module by name (the
layout is prescribed by design doc 4.1). Installed code is unaffected because all
imports are absolute, but running a file from inside this directory can break
dependencies that import stdlib ``platform``.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable

from . import ui

CursorReader = Callable[[], "tuple[int, int]"]

FORCE_X11_ENV = "SCREENRECON_FORCE_X11"
"""Set to 1 to attempt the X11 path even in a Wayland session."""


class CursorError(RuntimeError):
    """The cursor cannot be read on this machine at all. The message is user-facing."""


class CursorUnavailable(CursorError):
    """The cursor cannot be read at this moment, but may become readable again."""


_reader: CursorReader | None = None
_dpi_warning_shown = False


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_WINDOWS_ACCESS_DENIED = 5
_DPI_PER_MONITOR_AWARE = 2


def ensure_dpi_awareness() -> None:
    """Put this process in the physical-pixel coordinate space mss captures in.

    Call this before anything imports mss, so the coordinate space is established
    by us rather than by whichever module happens to load first.
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(  # type: ignore[attr-defined]
            _DPI_PER_MONITOR_AWARE
        )
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass

    # The call above is a no-op when awareness is already pinned (a manifest, or
    # the "Override high DPI scaling behavior" compatibility shim). That leaves
    # the process in virtualised coordinates, so say so rather than silently
    # capturing the wrong rectangle on a scaled display. Warn only once: the
    # reader is rebuilt periodically while the cursor is unreadable.
    global _dpi_warning_shown
    if _dpi_warning_shown:
        return
    try:
        awareness = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(  # type: ignore[attr-defined]
            None, ctypes.byref(awareness)
        )
        if awareness.value != _DPI_PER_MONITOR_AWARE:
            _dpi_warning_shown = True
            ui.warn(
                "This process is not per-monitor DPI aware, so coordinates may not match "
                "what is captured on a scaled display. Check for a high-DPI compatibility "
                "override on python.exe."
            )
    except Exception:
        pass


def _build_windows_reader() -> CursorReader:
    ensure_dpi_awareness()
    # use_last_error is required for ctypes.get_last_error() to report the real
    # Win32 error code rather than a stale value.
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]

    def read() -> tuple[int, int]:
        point = _POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            code = ctypes.get_last_error()
            if code == _WINDOWS_ACCESS_DENIED:
                raise CursorUnavailable(
                    "The screen is locked, or a UAC prompt or the secure desktop is showing."
                )
            raise CursorUnavailable(f"GetCursorPos failed (Windows error {code}).")
        return int(point.x), int(point.y)

    return read


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #


def _build_macos_reader() -> CursorReader:
    try:
        from Quartz import CGEventCreate, CGEventGetLocation  # type: ignore[import-not-found]
    except ImportError:
        raise CursorError(
            "Missing macOS dependency pyobjc-framework-Quartz.\n"
            "Install it with: pip install 'pyobjc-framework-Quartz'"
        ) from None

    def read() -> tuple[int, int]:
        event = CGEventCreate(None)
        if event is None:
            raise CursorUnavailable("CGEventCreate returned no event.")
        location = CGEventGetLocation(event)
        return int(location.x), int(location.y)

    return read


# --------------------------------------------------------------------------- #
# Linux / X11
# --------------------------------------------------------------------------- #


def _is_wayland() -> bool:
    """True for a Wayland session, including one running XWayland.

    Checking ``WAYLAND_DISPLAY and not DISPLAY`` is not enough: essentially every
    Wayland desktop also runs XWayland and sets DISPLAY. Taking the X11 path there
    is worse than failing — XQueryPointer returns stale coordinates whenever the
    pointer is over a native Wayland window, and mss captures a root window that
    contains only X11 clients, so the tool silently misbehaves.
    """
    if os.environ.get(FORCE_X11_ENV, "").strip() in ("1", "true", "yes"):
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland"


def _build_linux_reader() -> CursorReader:
    if _is_wayland():
        raise CursorError(
            "This is a Wayland session, which ScreenRecon cannot read the cursor from.\n"
            "XWayland does not help: it reports stale coordinates over native Wayland\n"
            "windows and captures them as black. Log in with an X11/Xorg session instead.\n"
            f"If your applications are all X11, you can override this with {FORCE_X11_ENV}=1."
        )
    try:
        from Xlib import display as xdisplay  # type: ignore[import-not-found]
    except ImportError:
        raise CursorError(
            "Missing Linux dependency python-xlib.\nInstall it with: pip install python-xlib"
        ) from None

    try:
        conn = xdisplay.Display()
    except Exception as exc:
        raise CursorError(
            f"Could not connect to the X11 display "
            f"(DISPLAY={os.environ.get('DISPLAY', 'unset')}): {exc}"
        ) from None
    # Only the default screen is tracked; a classic multi-screen :0.0/:0.1 layout
    # (as opposed to Xinerama) would report meaningless coordinates for :0.1.
    root = conn.screen().root

    def read() -> tuple[int, int]:
        try:
            data = root.query_pointer()
        except Exception as exc:
            # Recoverable: the caller can reset_reader() to rebuild the connection.
            raise CursorUnavailable(f"X11 query_pointer failed: {exc}") from None
        return int(data.root_x), int(data.root_y)

    return read


# --------------------------------------------------------------------------- #
# Public interface
# --------------------------------------------------------------------------- #


def _build_reader() -> CursorReader:
    if sys.platform == "win32":
        return _build_windows_reader()
    if sys.platform == "darwin":
        return _build_macos_reader()
    if sys.platform.startswith("linux"):
        return _build_linux_reader()
    raise CursorError(f"Unsupported platform: {sys.platform}")


def ensure_reader() -> None:
    """Bind the platform implementation, raising CursorError if this machine cannot work.

    Deliberately separate from reading a position: startup should fail loudly on
    a missing dependency, but must not fail merely because the screen is locked.
    """
    global _reader
    if _reader is None:
        _reader = _build_reader()


def get_cursor_pos() -> tuple[int, int]:
    """Return the current cursor position as (x, y).

    Raises CursorUnavailable when the cursor is temporarily unreadable, or
    CursorError when this machine cannot support the tool at all.
    """
    ensure_reader()
    assert _reader is not None
    return _reader()


def reset_reader() -> None:
    """Drop the bound implementation so the next call rebuilds it.

    Used to recover a dropped X11 connection, and by tests.
    """
    global _reader
    _reader = None


# --------------------------------------------------------------------------- #
# Monitor enumeration (used by the region picker and its default-centered fallback)
# --------------------------------------------------------------------------- #


def enumerate_monitors() -> list[dict[str, int]]:
    """Return the physical monitors as ``[{left, top, width, height}, ...]``.

    Coordinates are in the same virtual-desktop space as ``get_cursor_pos()``.
    The list excludes ``mss.monitors[0]`` (the union) — only real screens are
    returned. Empty list means mss reported no physical monitors, which should
    not happen on any supported platform.
    """
    ensure_dpi_awareness()
    import mss  # imported lazily; --help / --version must not load it

    factory = getattr(mss, "MSS", None) or mss.mss  # mss 11 drops the lowercase form
    with factory() as sct:
        return [
            {
                "left": int(m["left"]),
                "top": int(m["top"]),
                "width": int(m["width"]),
                "height": int(m["height"]),
            }
            for m in sct.monitors[1:]
        ]


def find_monitor_containing(x: int, y: int) -> dict[str, int] | None:
    """Return the monitor whose bounds contain ``(x, y)``, or ``None`` if none does."""
    for mon in enumerate_monitors():
        if (
            mon["left"] <= x < mon["left"] + mon["width"]
            and mon["top"] <= y < mon["top"] + mon["height"]
        ):
            return mon
    return None
