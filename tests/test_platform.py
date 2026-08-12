"""Cursor backend selection and failure classification (design doc 5.2).

The point of these tests is the distinction the watch loop depends on: a setup
failure is fatal, a momentarily unreadable cursor is not.
"""

from __future__ import annotations

import pytest

from screenrecon import platform


@pytest.fixture(autouse=True)
def _clear_bound_reader():
    platform.reset_reader()
    yield
    platform.reset_reader()


def test_unavailable_is_a_kind_of_cursor_error():
    """The loop catches CursorUnavailable first; anything else stays fatal."""
    assert issubclass(platform.CursorUnavailable, platform.CursorError)


def test_unsupported_platform_is_fatal(monkeypatch):
    monkeypatch.setattr(platform.sys, "platform", "sunos5")
    with pytest.raises(platform.CursorError) as excinfo:
        platform.ensure_reader()
    assert "Unsupported platform" in str(excinfo.value)
    assert not isinstance(excinfo.value, platform.CursorUnavailable)


def test_reader_is_built_once_and_reused(monkeypatch):
    builds = []

    def fake_build():
        builds.append(1)
        return lambda: (1, 2)

    monkeypatch.setattr(platform, "_build_reader", fake_build)
    assert platform.get_cursor_pos() == (1, 2)
    assert platform.get_cursor_pos() == (1, 2)
    assert len(builds) == 1


def test_reset_reader_forces_a_rebuild(monkeypatch):
    """Used by the watch loop to recover a dropped X11 connection."""
    builds = []

    def fake_build():
        builds.append(1)
        return lambda: (0, 0)

    monkeypatch.setattr(platform, "_build_reader", fake_build)
    platform.get_cursor_pos()
    platform.reset_reader()
    platform.get_cursor_pos()
    assert len(builds) == 2


def test_ensure_reader_does_not_read_the_cursor(monkeypatch):
    """Startup must not fail merely because the screen is locked right now."""

    def unreadable():
        raise platform.CursorUnavailable("locked")

    monkeypatch.setattr(platform, "_build_reader", lambda: unreadable)
    platform.ensure_reader()  # must not raise
    with pytest.raises(platform.CursorUnavailable):
        platform.get_cursor_pos()


# --------------------------------------------------------------------------- #
# Wayland detection (Linux)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, True),  # XWayland
        ({"WAYLAND_DISPLAY": "wayland-0"}, True),
        ({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, True),
        ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, False),
        ({"DISPLAY": ":0"}, False),
    ],
)
def test_wayland_detection(monkeypatch, env, expected):
    """XWayland sets both WAYLAND_DISPLAY and DISPLAY, and must still be refused."""
    for name in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE", platform.FORCE_X11_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert platform._is_wayland() is expected


def test_force_x11_overrides_wayland_detection(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv(platform.FORCE_X11_ENV, "1")
    assert platform._is_wayland() is False


def test_wayland_session_is_refused_with_guidance(monkeypatch):
    monkeypatch.setattr(platform.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv(platform.FORCE_X11_ENV, raising=False)

    with pytest.raises(platform.CursorError) as excinfo:
        platform.ensure_reader()
    message = str(excinfo.value)
    assert "Wayland" in message
    assert "XWayland" in message  # the trap this guard exists to prevent
