"""Capture conversion, region sanity checks and downscaling (design doc 5.3)."""

from __future__ import annotations

import pytest
from PIL import Image

from screenrecon import capture

SCREEN = {"left": 0, "top": 0, "width": 1920, "height": 1080}


def solid(size=(4, 4), colour=(30, 30, 30)) -> Image.Image:
    return Image.new("RGB", size, colour)


# --------------------------------------------------------------------------- #
# Blank detection
# --------------------------------------------------------------------------- #


def test_flat_image_looks_blank():
    assert capture.looks_blank(solid()) is True


def test_image_with_content_does_not_look_blank():
    image = solid()
    image.putpixel((0, 0), (255, 255, 255))
    assert capture.looks_blank(image) is False


def test_looks_blank_never_raises_on_a_bad_image():
    assert capture.looks_blank(object()) is False


# --------------------------------------------------------------------------- #
# Region sanity (a region off-screen captures black, it does not error)
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_screen(monkeypatch):
    monkeypatch.setattr(capture, "screen_bounds", lambda: dict(SCREEN))


def test_region_inside_the_screen_is_fine(fake_screen):
    assert capture.check_region({"left": 100, "top": 100, "width": 600, "height": 400}) is None


def test_region_touching_the_edges_is_fine(fake_screen):
    assert capture.check_region({"left": 0, "top": 0, "width": 1920, "height": 1080}) is None


def test_region_entirely_off_screen_is_reported(fake_screen):
    problem = capture.check_region({"left": 3000, "top": 100, "width": 200, "height": 150})
    assert problem is not None
    assert "entirely" in problem


def test_region_at_negative_coordinates_off_screen_is_reported(fake_screen):
    problem = capture.check_region({"left": -500, "top": 0, "width": 200, "height": 150})
    assert problem is not None


def test_region_partly_off_screen_is_reported(fake_screen):
    problem = capture.check_region({"left": 1800, "top": 0, "width": 400, "height": 150})
    assert problem is not None
    assert "past the edge" in problem


def test_check_region_is_silent_when_the_screen_is_unknown(monkeypatch):
    monkeypatch.setattr(capture, "screen_bounds", lambda: None)
    assert capture.check_region({"left": 9999, "top": 9999, "width": 10, "height": 10}) is None


# --------------------------------------------------------------------------- #
# Downscaling for the vision API
# --------------------------------------------------------------------------- #


def test_small_images_are_untouched():
    image = solid((800, 600))
    assert capture.downscale_for_api(image) is image


def test_oversized_images_are_scaled_to_the_long_edge():
    image = solid((5120, 1440))
    result = capture.downscale_for_api(image)
    assert max(result.size) == capture.MAX_IMAGE_EDGE
    # Aspect ratio preserved within a rounding pixel.
    assert abs(result.size[0] / result.size[1] - 5120 / 1440) < 0.01


def test_downscaling_keeps_a_tall_image_tall():
    result = capture.downscale_for_api(solid((100, 4000)))
    assert result.size[1] == capture.MAX_IMAGE_EDGE
    assert result.size[0] >= 1


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def test_to_jpeg_bytes_produces_a_jpeg():
    data = capture.to_jpeg_bytes(solid())
    # SOI marker for every JPEG file.
    assert data.startswith(b"\xff\xd8\xff")


def test_encode_failure_becomes_a_capture_error():
    with pytest.raises(capture.CaptureError):
        capture.to_jpeg_bytes(object())


def test_jpeg_encoder_handles_rgba_input():
    """mss on some backends returns RGBA; JPEG has no alpha and must not raise."""
    rgba = Image.new("RGBA", (4, 4), (10, 20, 30, 200))
    data = capture.to_jpeg_bytes(rgba)
    assert data.startswith(b"\xff\xd8\xff")


def test_bgra_conversion_does_not_swap_red_and_blue():
    """mss delivers BGRA; the raw mode must read it back as RGB, not BGR."""
    # Pixels: pure blue, pure red, pure green, and one arbitrary colour.
    bgra = bytes([255, 0, 0, 255, 0, 0, 255, 255, 0, 255, 0, 255, 10, 20, 30, 255])
    image = Image.frombytes("RGB", (4, 1), bgra, "raw", "BGRX")
    assert list(image.getdata()) == [(0, 0, 255), (255, 0, 0), (0, 255, 0), (30, 20, 10)]
