"""The four outputs of one trigger are independent (design doc 4.2).

This is the reliability promise the README leads with: a Telegram outage must not
stop the local archive, and a full disk must not stop the Telegram push.
"""

from __future__ import annotations

import pytest
from PIL import Image

from screenrecon import capture, notify, storage, vision, watcher


@pytest.fixture
def cfg(tmp_path):
    return {
        "region": {"left": 0, "top": 0, "width": 10, "height": 10},
        "anthropic_api_key": "sk-ant-fake-key-value",
        "telegram_bot_token": "123456:fake-bot-token",
        "telegram_chat_id": "987654321",
        "save_dir": str(tmp_path / "archive"),
        "model": "claude-opus-5",
        "dwell_seconds": 3,
    }


@pytest.fixture
def stubs(monkeypatch):
    """Replace every outward-facing call with a recording stub."""
    calls: dict[str, list] = {"vision": [], "notify": []}

    monkeypatch.setattr(capture, "grab", lambda region: Image.new("RGB", (10, 10), (1, 2, 3)))
    monkeypatch.setattr(watcher.capture, "grab", capture.grab)
    def fake_stream(cfg, turns, on_delta):
        # Record the last user turn's text so tests can assert on it.
        calls["vision"].append(turns[-1].text)
        on_delta("the answer")
        return vision.Reply(True, "the answer")

    monkeypatch.setattr(vision, "ask_streaming", fake_stream)
    monkeypatch.setattr(
        notify,
        "send",
        lambda token, chat, png, text: calls["notify"].append(text) or True,
    )
    return calls


def archive_files(cfg):
    directory = storage.normalise_dir(cfg["save_dir"])
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_one_trigger_produces_all_four_outputs(cfg, stubs, capsys):
    assert watcher.handle_trigger(cfg, "read this") is True

    assert stubs["vision"] == ["read this"]
    assert stubs["notify"] == ["the answer"]
    assert "the answer" in capsys.readouterr().out

    files = archive_files(cfg)
    assert len(files) == 2
    assert files[0].endswith(".jpg") and files[1].endswith(".txt")


# --------------------------------------------------------------------------- #
# Independence of the outputs
# --------------------------------------------------------------------------- #


def test_telegram_failure_does_not_stop_the_local_archive(cfg, stubs, monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)
    assert watcher.handle_trigger(cfg, "read this") is True
    assert len(archive_files(cfg)) == 2


def test_telegram_raising_does_not_escape_the_trigger(cfg, stubs, monkeypatch):
    """notify promises never to raise; if it ever does, the loop still survives."""

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(notify, "send", explode)
    with pytest.raises(RuntimeError):
        watcher.handle_trigger(cfg, "read this")
    # ...and run() is the layer that contains it — see test_watch_loop_survives_*.


def test_unusable_save_dir_does_not_stop_telegram(cfg, stubs, monkeypatch):
    monkeypatch.setattr(
        storage, "resolve_dir", lambda path: (_ for _ in ()).throw(OSError(13, "Permission denied"))
    )
    assert watcher.handle_trigger(cfg, "read this") is True
    assert stubs["notify"] == ["the answer"]


def test_missing_home_directory_does_not_stop_telegram(cfg, stubs, monkeypatch):
    """Path.expanduser raises RuntimeError, not OSError, when there is no home."""
    monkeypatch.setattr(
        storage,
        "resolve_dir",
        lambda path: (_ for _ in ()).throw(RuntimeError("Could not determine home directory")),
    )
    assert watcher.handle_trigger(cfg, "read this") is True
    assert stubs["notify"] == ["the answer"]


def test_failed_screenshot_write_leaves_no_orphan_text(cfg, stubs, monkeypatch):
    """The .txt is the companion of the .jpg; alone it is an orphan."""
    monkeypatch.setattr(storage, "save_jpeg", lambda directory, stem, data: None)
    assert watcher.handle_trigger(cfg, "read this") is True
    assert archive_files(cfg) == []


def test_capture_failure_reports_and_returns_false(cfg, stubs, monkeypatch, capsys):
    monkeypatch.setattr(
        capture, "grab", lambda region: (_ for _ in ()).throw(capture.CaptureError("no display"))
    )
    assert watcher.handle_trigger(cfg, "read this") is False
    assert "no display" in capsys.readouterr().err


def test_recognition_failure_is_still_archived_and_pushed(cfg, stubs, monkeypatch):
    monkeypatch.setattr(
        vision,
        "ask_streaming",
        lambda *a, **k: vision.Reply(False, "Network connection failed."),
    )
    assert watcher.handle_trigger(cfg, "read this") is False
    assert len(archive_files(cfg)) == 2
    assert stubs["notify"] == ["(recognition failed) Network connection failed."]


# --------------------------------------------------------------------------- #
# The watch loop
# --------------------------------------------------------------------------- #


class LoopClock:
    """A monotonic clock the test advances by one poll per read.

    Never exhausts, so unrelated calls to time.monotonic during the test cannot
    make the loop behave differently than the script says.
    """

    def __init__(self, step: float = 0.1, start: float = 1000.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def jump(self, seconds: float) -> None:
        self.now += seconds


class Locked(Exception):
    """A scripted period during which the cursor cannot be read."""

    def __init__(self, seconds: float) -> None:
        super().__init__("The screen is locked")
        self.seconds = seconds


def run_loop(cfg, monkeypatch, script, *, on_trigger=None):
    """Drive watcher.run over a scripted sequence of cursor reads.

    Each entry is an (x, y) position or a Locked(seconds). The loop ends with a
    KeyboardInterrupt once the script runs out, mimicking Ctrl+C.
    """
    from screenrecon import platform

    fired: list[str] = []
    remaining = list(script)
    clock = LoopClock()

    def next_position():
        if not remaining:
            raise KeyboardInterrupt
        item = remaining.pop(0)
        if isinstance(item, Locked):
            clock.jump(item.seconds)
            raise platform.CursorUnavailable(str(item))
        return item

    monkeypatch.setattr(platform, "ensure_reader", lambda: None)
    monkeypatch.setattr(platform, "get_cursor_pos", next_position)
    monkeypatch.setattr(watcher, "check_environment", lambda cfg: None)
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(watcher.time, "monotonic", clock)
    monkeypatch.setattr(
        watcher,
        "handle_trigger",
        on_trigger or (lambda cfg, prompt: fired.append(prompt) or True),
    )
    exit_code = watcher.run(cfg, "prompt")
    return fired, exit_code


def test_dwell_fires_once_after_the_configured_time(cfg, monkeypatch):
    """Sanity check that the harness can produce a trigger at all."""
    fired, exit_code = run_loop(cfg, monkeypatch, [(5, 5)] * 60)
    assert fired == ["prompt"]
    assert exit_code == 0


def test_unavailable_cursor_does_not_bank_dwell_time(cfg, monkeypatch):
    """A locked screen must not bank dwell time.

    The cursor sits inside the region, the screen locks for an hour, and on
    unlock it is still inside. Dwell is *continuous* time inside the region, and
    continuity across an unobserved hour cannot be claimed — firing here would
    capture and upload the screen the user has only just unlocked.
    """
    script = [(5, 5), (5, 5), Locked(3600.0), (5, 5), (5, 5)]
    fired, _ = run_loop(cfg, monkeypatch, script)
    assert fired == []


def test_dwell_restarts_cleanly_after_the_cursor_returns(cfg, monkeypatch):
    """After the gap the timer starts again from zero, and can still fire."""
    script = [(5, 5), Locked(3600.0)] + [(5, 5)] * 60
    fired, _ = run_loop(cfg, monkeypatch, script)
    assert fired == ["prompt"]


def test_watch_loop_survives_a_failing_trigger(cfg, monkeypatch, capsys):
    """NFR-2: one bad trigger must never take the watcher down."""

    def explode(cfg, prompt):
        raise RuntimeError(f"boom {cfg['anthropic_api_key']}")

    fired, exit_code = run_loop(cfg, monkeypatch, [(5, 5)] * 80, on_trigger=explode)
    assert exit_code == 0  # ended on Ctrl+C, not on the exception

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Trigger failed" in combined
    assert cfg["anthropic_api_key"] not in combined


def test_credentials_never_reach_the_terminal(cfg, stubs, capsys):
    watcher.handle_trigger(cfg, "read this")
    output = capsys.readouterr()
    combined = output.out + output.err
    assert cfg["anthropic_api_key"] not in combined
    assert cfg["telegram_bot_token"] not in combined
