"""Display geometry — monitor enumeration and virtual-desktop bounds (design doc 5.10).

Split out from ``platform.py`` in SR-7 so the cursor path (a tiny ctypes /
pyobjc / xlib layer) no longer implicitly depends on mss. Both live behind the
same coordinate contract — read on Windows after ``platform.ensure_dpi_awareness()``
so pixels here match pixels there — but they are otherwise independent.

Callers: the region picker (``picker.py``) and the wizard's post-pick "on
monitor N of M" report. The capture path (``capture.py``) opens its own mss
context per frame and does not need this module.
"""

from __future__ import annotations


def _mss_factory():
    """Construct an mss instance across versions (``mss.mss`` is dropped in mss 11)."""
    import mss  # imported lazily; --help / --version must not load it

    return mss.MSS if hasattr(mss, "MSS") else mss.mss


def _read_monitors() -> list[dict[str, int]]:
    """Return ``sct.monitors`` as plain ints — index 0 is the virtual-desktop union."""
    from . import platform as cursor_platform  # for DPI awareness

    cursor_platform.ensure_dpi_awareness()
    with _mss_factory()() as sct:
        return [
            {
                "left": int(m["left"]),
                "top": int(m["top"]),
                "width": int(m["width"]),
                "height": int(m["height"]),
            }
            for m in sct.monitors
        ]


def enumerate_monitors() -> list[dict[str, int]]:
    """Return the physical monitors as ``[{left, top, width, height}, ...]``.

    Coordinates are in the same virtual-desktop space as ``platform.get_cursor_pos()``.
    Excludes ``mss.monitors[0]`` (the union). Empty list means mss reported no
    physical monitors, which should not happen on any supported platform.
    """
    return _read_monitors()[1:]


def virtual_desktop_bounds() -> dict[str, int]:
    """Return the union of every monitor as ``{left, top, width, height}``.

    Used by the region picker to size a single overlay across every screen.
    Centralised here so the picker inherits DPI awareness and the mss-version
    shim without opening its own mss context.
    """
    return _read_monitors()[0]


def find_monitor_index_containing(
    x: int, y: int, monitors: list[dict[str, int]] | None = None
) -> tuple[int, dict[str, int]] | None:
    """Return ``(1-based-index, monitor)`` for the monitor containing ``(x, y)``,
    or ``None`` if none does. Callers that need the index for user-facing text
    ("on monitor 2 of 3") should use this rather than ``find_monitor_containing``
    + ``list.index``, which relies on the returned dict being the same object as
    an entry in the passed-in list.

    Pass ``monitors`` to reuse an already-fetched list; otherwise this function
    fetches one itself.
    """
    for index, mon in enumerate(
        monitors if monitors is not None else enumerate_monitors(), start=1
    ):
        if (
            mon["left"] <= x < mon["left"] + mon["width"]
            and mon["top"] <= y < mon["top"] + mon["height"]
        ):
            return index, mon
    return None


def find_monitor_containing(
    x: int, y: int, monitors: list[dict[str, int]] | None = None
) -> dict[str, int] | None:
    """Return the monitor whose bounds contain ``(x, y)``, or ``None`` if none does.

    Pass ``monitors`` to reuse an already-fetched list; otherwise this function
    fetches one itself (each fetch spins up a fresh mss instance).
    """
    result = find_monitor_index_containing(x, y, monitors)
    return None if result is None else result[1]
