"""Region capture (design doc 5.3).

Grabs with mss and converts to a ``PIL.Image`` (RGB). A fresh ``mss.mss()`` is
created per capture inside a ``with`` block — mss instances are thread-bound on
macOS, and this keeps the instance on the calling thread.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Mapping
from typing import Any

MACOS_PERMISSION_HINT = (
    "macOS screen recording permission is missing, so captures show only the desktop\n"
    "wallpaper and the menu bar. Grant it under:\n"
    "  System Settings > Privacy & Security > Screen Recording > enable your terminal\n"
    "Then quit the terminal completely and reopen it for the change to take effect."
)

FLAT_CAPTURE_HINT = (
    "The capture is a single flat colour. If that is not what the region contains,\n"
    "re-run 'screenrecon --configure' and pick the region again."
)

MAX_IMAGE_EDGE = 2576
"""Longest edge the vision models accept before downscaling server-side. Resizing
here instead keeps control of the resampling and avoids uploading wasted pixels."""


class CaptureError(RuntimeError):
    """Capture failed. The message is safe to show the user."""


def grab(region: Mapping[str, Any]) -> Any:
    """Capture ``region`` and return an RGB ``PIL.Image``.

    ``region`` uses the same coordinate space as ``platform.get_cursor_pos()``.
    The returned pixel size is not authoritative — never write it back onto
    ``region`` (design doc 5.1, note 3). On a Retina Mac the capture comes back
    at 1x logical resolution, because the backend requests nominal resolution;
    see :func:`retina_note`.
    """
    try:
        import mss  # imported lazily: --help / --configure do not need it
        from PIL import Image
    except ImportError as exc:
        raise CaptureError(
            f"Missing capture dependency ({exc.name}). Install with: pip install screenrecon"
        ) from None

    box = {
        "left": int(region["left"]),
        "top": int(region["top"]),
        "width": int(region["width"]),
        "height": int(region["height"]),
    }
    try:
        with _mss_instance(mss) as sct:
            raw = sct.grab(box)
        # Kept inside the try: a size/stride mismatch raises ValueError here, and
        # it must surface as a CaptureError like every other capture failure.
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception as exc:
        raise CaptureError(_translate_capture_error(exc)) from None


def _mss_instance(mss_module: Any) -> Any:
    """Construct an mss instance across versions.

    ``mss.mss`` is deprecated in mss 10 and removed in 11; ``mss.MSS`` is the
    replacement. Prefer the new name and fall back for older releases.
    """
    factory = getattr(mss_module, "MSS", None) or mss_module.mss
    return factory()


def screen_bounds() -> dict[str, int] | None:
    """Bounding rectangle of the whole virtual desktop, or None if unavailable."""
    try:
        import mss

        with _mss_instance(mss) as sct:
            virtual = sct.monitors[0]
    except Exception:
        return None
    return {
        "left": int(virtual["left"]),
        "top": int(virtual["top"]),
        "width": int(virtual["width"]),
        "height": int(virtual["height"]),
    }


def check_region(region: Mapping[str, Any]) -> str | None:
    """Describe how ``region`` sits against the real screen, or None if it is fine.

    A region outside the desktop is not an error for mss — it returns black
    padding — so without this check a mistyped coordinate means every trigger
    silently uploads a black image.
    """
    bounds = screen_bounds()
    if bounds is None:
        return None
    left, top = int(region["left"]), int(region["top"])
    right, bottom = left + int(region["width"]), top + int(region["height"])
    screen_right = bounds["left"] + bounds["width"]
    screen_bottom = bounds["top"] + bounds["height"]

    disjoint = (
        right <= bounds["left"]
        or left >= screen_right
        or bottom <= bounds["top"]
        or top >= screen_bottom
    )
    if disjoint:
        return (
            f"The region ({left},{top} {region['width']}x{region['height']}) lies entirely "
            f"outside the screen ({bounds['left']},{bounds['top']} "
            f"{bounds['width']}x{bounds['height']}). Captures would be blank."
        )
    clipped = (
        left < bounds["left"]
        or top < bounds["top"]
        or right > screen_right
        or bottom > screen_bottom
    )
    if clipped:
        return "The region extends past the edge of the screen; the area outside will be blank."
    return None


def downscale_for_api(image: Any) -> Any:
    """Shrink an oversized capture so the API does not downscale it for us."""
    width, height = image.size
    longest = max(width, height)
    if longest <= MAX_IMAGE_EDGE:
        return image
    try:
        from PIL import Image

        scale = MAX_IMAGE_EDGE / longest
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        return image.resize(size, Image.LANCZOS)
    except Exception:
        return image


def _translate_capture_error(exc: Exception) -> str:
    text = str(exc)
    if sys.platform == "darwin" and macos_permission_missing():
        return f"Screen capture failed: {text}\n{MACOS_PERMISSION_HINT}"
    if sys.platform.startswith("linux"):
        return (
            f"Screen capture failed: {text}\n"
            "Only X11 is supported on Linux; switch to an X11/Xorg session if you are on Wayland."
        )
    return f"Screen capture failed: {text}"


JPEG_QUALITY = 90
"""JPEG quality for archive and API upload. 90 keeps UI screenshots visually
lossless for OCR / description tasks while producing files roughly 5-10× smaller
than the equivalent PNG. Not exposed as config — one quality for one use case."""


def to_jpeg_bytes(image: Any, quality: int = JPEG_QUALITY) -> bytes:
    """Encode a ``PIL.Image`` as JPEG bytes. Raises CaptureError on encode failure.

    JPEG has no alpha channel and demands an RGB image; the ``convert`` guards a
    grabber that returned RGBA (some mss backends do on transparent overlays).
    ``optimize=True`` runs the extra Huffman pass — cheap on modern CPUs and
    trims another few percent off the size.
    """
    buffer = io.BytesIO()
    try:
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
    except Exception as exc:
        raise CaptureError(f"Could not encode the capture as JPEG: {exc}") from None
    return buffer.getvalue()


def looks_blank(image: Any) -> bool:
    """True when the capture is a single flat colour.

    A weak signal on its own — a blank editor pane is legitimately flat — so this
    only ever produces a one-shot hint, never an error.
    """
    try:
        extrema = image.convert("RGB").getextrema()
    except Exception:
        return False
    return all(channel_min == channel_max for channel_min, channel_max in extrema)


def retina_note() -> str | None:
    """Warn once on a Retina Mac that captures are taken at 1x, or None.

    The capture backend requests nominal resolution, so on a 2x display the
    image has half the detail the panel shows. The user this hurts is the one
    wondering why small text is misread, so tell them at startup rather than
    only in the README.
    """
    if sys.platform != "darwin":
        return None
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGDisplayBounds,
            CGDisplayPixelsWide,
            CGMainDisplayID,
        )

        display = CGMainDisplayID()
        logical = CGDisplayBounds(display).size.width
        physical = CGDisplayPixelsWide(display)
        if not logical or physical / logical <= 1.0:
            return None
    except Exception:
        return None
    return (
        "This is a Retina display, and captures are taken at 1x logical resolution, "
        "so very small text may be hard to read. Use a tighter region if the model "
        "misreads it."
    )


def macos_permission_missing() -> bool:
    """True when macOS screen recording permission has not been granted.

    Since macOS 10.15 an unauthorised capture returns the wallpaper and menu bar
    rather than failing or returning black, so the permission cannot be inferred
    from the pixels — it has to be asked for directly.
    """
    if sys.platform != "darwin":
        return False
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGPreflightScreenCaptureAccess,
        )
    except ImportError:
        return False
    try:
        return not CGPreflightScreenCaptureAccess()
    except Exception:
        return False
