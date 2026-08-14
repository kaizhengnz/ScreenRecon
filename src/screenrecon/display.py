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


def resolve_monitor_info(
    region: dict[str, int], monitors: list[dict[str, int]] | None = None
) -> dict[str, int] | None:
    """Return ``{"index": N, "of": M}`` for the monitor best associated with
    ``region`` — the one containing its centre, or, failing that, the nearest
    one by squared distance to its rectangle. ``None`` when there is nothing
    to say (headless, no monitors reported, or an incomplete region dict).

    Meant to be called *once*, at the moment the user picks a region, and the
    result stored in the config so the watcher displays exactly what was
    chosen. Recomputing at watch time would drift as monitors are plugged /
    unplugged or as `mss` reports them differently across DPI-awareness
    contexts (the SR-20 class of confusion).
    """
    left = region.get("left")
    top = region.get("top")
    width = region.get("width")
    height = region.get("height")
    if not all(isinstance(v, int) for v in (left, top, width, height)):
        return None
    if monitors is None:
        try:
            monitors = enumerate_monitors()
        except Exception:
            return None
    if not monitors:
        return None
    cx = left + width // 2  # type: ignore[operator]
    cy = top + height // 2  # type: ignore[operator]
    found = find_monitor_index_containing(cx, cy, monitors)
    index = found[0] if found is not None else _nearest_monitor_index(cx, cy, monitors)
    return {"index": index, "of": len(monitors)}


def format_monitor_info(info: dict[str, int] | None) -> str:
    """Render stored monitor info as `` (on monitor N of M)``, or empty string.

    Keeps rendering out of the config layer: config holds `{"index", "of"}`,
    display owns the human string. Empty when ``info`` is ``None`` or is
    missing either key.
    """
    if not isinstance(info, dict):
        return ""
    index = info.get("index")
    of = info.get("of")
    if not isinstance(index, int) or not isinstance(of, int):
        return ""
    return f" (on monitor {index} of {of})"


def describe_region_monitor(
    region: dict[str, int], monitors: list[dict[str, int]] | None = None
) -> str:
    """Compute the monitor annotation for ``region`` on the fly.

    Convenience wrapper for the pre-``monitor``-field configs and for the
    picker's post-pick "Picked: ... (on monitor N of M)" line. New code
    that has a stored monitor info dict should call
    :func:`format_monitor_info` on that dict instead — the stored value is
    what the user chose, and does not drift with topology changes.
    """
    return format_monitor_info(resolve_monitor_info(region, monitors))


def _nearest_monitor_index(cx: int, cy: int, monitors: list[dict[str, int]]) -> int:
    """Return the 1-based index of the monitor whose rectangle is closest to
    ``(cx, cy)``. Distance is squared distance to the nearest edge (0 when the
    point is inside), so no square root. Ties break to the earlier monitor.
    """
    def squared_distance(m: dict[str, int]) -> int:
        dx = max(m["left"] - cx, 0, cx - (m["left"] + m["width"]))
        dy = max(m["top"] - cy, 0, cy - (m["top"] + m["height"]))
        return dx * dx + dy * dy

    best_index = 1
    best_distance = squared_distance(monitors[0])
    for i, m in enumerate(monitors[1:], start=2):
        d = squared_distance(m)
        if d < best_distance:
            best_distance = d
            best_index = i
    return best_index


def find_monitor_containing(
    x: int, y: int, monitors: list[dict[str, int]] | None = None
) -> dict[str, int] | None:
    """Return the monitor whose bounds contain ``(x, y)``, or ``None`` if none does.

    Pass ``monitors`` to reuse an already-fetched list; otherwise this function
    fetches one itself (each fetch spins up a fresh mss instance).
    """
    result = find_monitor_index_containing(x, y, monitors)
    return None if result is None else result[1]
