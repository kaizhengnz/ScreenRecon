"""Main loop and dwell trigger state machine (design doc 5.1) — the core logic."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from typing import Any

from . import capture, notify, platform, storage, ui, vision

CREDENTIAL_KEYS = ("anthropic_api_key", "telegram_bot_token", "telegram_chat_id")

POLL_INTERVAL = 0.1
"""Polling period in seconds. NFR-1 requires <= 200 ms."""

UNAVAILABLE_POLL_INTERVAL = 1.0
"""Slower poll while the cursor is unreadable — the screen may be locked for hours."""

CURSOR_REBUILD_AFTER = 30
"""Consecutive failures before rebuilding the platform reader, which recovers a
dropped X11 connection."""

_blank_hint_shown = False


def _secrets(cfg: Mapping[str, Any]) -> list[str]:
    """Credential values to strip from any message that quotes a third party."""
    return [str(cfg.get(key) or "") for key in CREDENTIAL_KEYS]


class DwellTrigger:
    """Mouse dwell trigger state machine.

    - Always driven by ``time.monotonic()``: wall-clock jumps would cause spurious
      triggers.
    - After firing, the trigger is disarmed and only re-arms once the cursor
      leaves the region (FR-10).
    """

    def __init__(self, region: Mapping[str, Any], dwell_seconds: float) -> None:
        self.left = int(region["left"])
        self.top = int(region["top"])
        self.width = int(region["width"])
        self.height = int(region["height"])
        self.dwell_seconds = float(dwell_seconds)
        self.entered_at: float | None = None
        self.armed = True

    def contains(self, position: tuple[int, int]) -> bool:
        """Half-open interval test: left <= x < left + width."""
        x, y = position
        return (
            self.left <= x < self.left + self.width
            and self.top <= y < self.top + self.height
        )

    def dwelled_for(self, now: float) -> float:
        """Seconds dwelled during the current entry; 0 when outside the region."""
        if self.entered_at is None:
            return 0.0
        return max(0.0, now - self.entered_at)

    def update(self, position: tuple[int, int], now: float) -> bool:
        """Advance one polling period; returns whether to fire this period."""
        if not self.contains(position):
            self.entered_at = None
            self.armed = True  # leaving the region re-arms
            return False
        if not self.armed:
            return False
        if self.entered_at is None:
            self.entered_at = now
            return False
        if now - self.entered_at >= self.dwell_seconds:
            self.armed = False  # disarm before handling, so handling cannot re-trigger
            return True
        return False


# --------------------------------------------------------------------------- #
# One full trigger (data flow: design doc 4.2)
# --------------------------------------------------------------------------- #


def _warn_if_blank(image: Any) -> None:
    """One-shot hint when a capture is a flat colour, on any platform."""
    global _blank_hint_shown
    if _blank_hint_shown:
        return
    if capture.looks_blank(image):
        _blank_hint_shown = True
        # Ask the system rather than assuming: a macOS user who has granted
        # permission and is watching a legitimately flat region should not be
        # told to fix a permission that is already correct.
        ui.warn(
            capture.MACOS_PERMISSION_HINT
            if capture.macos_permission_missing()
            else capture.FLAT_CAPTURE_HINT
        )


def check_environment(cfg: Mapping[str, Any] | None) -> None:
    """Warn at startup about conditions that would silently produce useless captures."""
    if capture.macos_permission_missing():
        ui.warn(capture.MACOS_PERMISSION_HINT)
    note = capture.retina_note()
    if note:
        ui.warn(note)
    region = (cfg or {}).get("region")
    if isinstance(region, dict):
        problem = capture.check_region(region)
        if problem:
            ui.warn(problem)


def print_destinations(cfg: Mapping[str, Any]) -> None:
    """Show where captures will go, so a config you did not write cannot hide it.

    The archive path is the *resolved* one actually written to: a `save_dir` of
    ``~/ScreenRecon/../../Public`` would otherwise display a reassuring prefix
    while writing somewhere else entirely.
    """
    try:
        archive: Any = storage.normalise_dir(str(cfg["save_dir"]))
    except (OSError, RuntimeError, ValueError):
        archive = cfg["save_dir"]
    ui.info(f"Archive: {archive}")
    ui.info(f"Telegram chat: {ui.mask(str(cfg['telegram_chat_id']))}")


def _encode(image: Any) -> tuple[bytes, bytes]:
    """Return (png to archive, png to upload).

    The archive keeps full resolution; only the uploaded copy is downscaled, and
    only when it exceeds what the vision models accept. When no downscaling is
    needed both are the same bytes, so the common case encodes once.
    """
    api_image = capture.downscale_for_api(image)
    archive_png = capture.to_png_bytes(image)
    if api_image is image:
        return archive_png, archive_png
    return archive_png, capture.to_png_bytes(api_image)


def handle_trigger(cfg: Mapping[str, Any], prompt: str) -> bool:
    """Capture, archive, ask the AI, print, save text, push to Telegram.

    The output steps are independent: a Telegram failure does not affect the local
    archive and vice versa. Returns whether recognition succeeded.
    """
    local_start = time.monotonic()
    try:
        image = capture.grab(cfg["region"])
        _warn_if_blank(image)
        archive_png, api_png = _encode(image)
    except capture.CaptureError as exc:
        ui.error(str(exc))
        return False

    directory = None
    stem = ""
    saved_png = None
    try:
        directory = storage.resolve_dir(str(cfg["save_dir"]))
        stem = storage.new_stem(directory)
        saved_png = storage.save_png(directory, stem, archive_png)
    except (OSError, RuntimeError) as exc:
        # RuntimeError: Path.expanduser() when the platform reports no home dir.
        ui.warn(f"Save directory unusable ({cfg['save_dir']}): {exc}")

    local_ms = (time.monotonic() - local_start) * 1000

    api_start = time.monotonic()
    reply = vision.ask_image(
        str(cfg["anthropic_api_key"]), str(cfg["model"]), api_png, prompt
    )
    api_seconds = time.monotonic() - api_start

    text = reply.text if reply.ok else f"(recognition failed) {reply.text}"
    ui.rule(time.strftime("%H:%M:%S"))
    ui.info(text)
    ui.info(f"\n(local {local_ms:.0f} ms, API {api_seconds:.1f} s)")

    # The .txt is the companion of the .png; without the image it is an orphan.
    if saved_png is not None and directory is not None and stem:
        storage.save_txt(directory, stem, text)

    notify.send(
        str(cfg["telegram_bot_token"]), str(cfg["telegram_chat_id"]), api_png, text
    )
    return reply.ok


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def _format_region(region: Mapping[str, Any]) -> str:
    return (
        f"left={region['left']} top={region['top']} "
        f"width={region['width']} height={region['height']}"
    )


def run(cfg: Mapping[str, Any], prompt: str) -> int:
    """Run the watch loop. Returns the process exit code."""
    trigger = DwellTrigger(cfg["region"], cfg["dwell_seconds"])

    try:
        # Only a setup failure is fatal. A locked screen at startup is not.
        platform.ensure_reader()
    except platform.CursorError as exc:
        ui.error(str(exc))
        return 1

    check_environment(cfg)

    ui.rule("ScreenRecon watching")
    ui.info(f"Region: {_format_region(cfg['region'])}")
    ui.info(f"Fires after {cfg['dwell_seconds']}s dwell | model {cfg['model']}")
    print_destinations(cfg)
    ui.info(f"Prompt: {prompt}")
    ui.info("Press Ctrl+C to quit.\n")

    failures = 0
    try:
        while True:
            try:
                position = platform.get_cursor_pos()
                if failures:
                    ui.info("Cursor readable again, watching.")
                failures = 0
            except platform.CursorUnavailable as exc:
                # Transient by definition: the screen is locked, a UAC prompt is
                # up, or X hiccuped. Wait it out however long it takes.
                failures += 1
                # Dwell is *continuous* time inside the region, and continuity
                # cannot be claimed across a gap we could not observe. Without
                # this, a cursor that was inside the region when the screen
                # locked fires the moment it unlocks.
                trigger.entered_at = None
                if failures == 1:
                    ui.warn(f"Cursor unreadable, waiting: {exc}")
                if failures % CURSOR_REBUILD_AFTER == 0:
                    platform.reset_reader()  # recover a dropped X11 connection
                time.sleep(UNAVAILABLE_POLL_INTERVAL)
                continue
            except platform.CursorError as exc:
                ui.error(str(exc))
                return 1

            if trigger.update(position, time.monotonic()):
                try:
                    handle_trigger(cfg, prompt)
                except Exception as exc:
                    # NFR-2: one bad trigger must never take the watcher down.
                    ui.error(
                        ui.scrub(
                            f"Trigger failed: {type(exc).__name__}: {exc}", _secrets(cfg)
                        )
                    )
                ui.info("Move the cursor out of the region to arm the next trigger...")

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        ui.info("\nScreenRecon stopped.")
        return 0


def run_show_cursor(cfg: Mapping[str, Any] | None = None) -> int:
    """Print live cursor coordinates to help pick a region (FR-12)."""
    region = (cfg or {}).get("region") if cfg else None
    trigger = None
    if isinstance(region, dict):
        try:
            trigger = DwellTrigger(region, 1)
        except (KeyError, TypeError, ValueError):
            trigger = None

    try:
        platform.ensure_reader()
    except platform.CursorError as exc:
        ui.error(str(exc))
        return 1

    # This is the mode users are in while choosing coordinates, so an off-screen
    # region or a missing permission is most actionable here.
    check_environment(cfg)

    ui.rule("Cursor position (Ctrl+C to quit)")
    if trigger is not None:
        ui.info(f"Configured region: {_format_region(region)}")
    ui.info("Point at the top-left and bottom-right of your target area and note both readings.\n")

    # Overwriting one line only makes sense on a terminal; when redirected it
    # would produce a single enormous line.
    interactive = sys.stdout.isatty()
    waiting = False

    try:
        while True:
            try:
                x, y = platform.get_cursor_pos()
            except platform.CursorUnavailable as exc:
                if not waiting:
                    if interactive:
                        print()
                    ui.warn(f"Cursor unreadable, waiting: {exc}")
                    waiting = True
                time.sleep(UNAVAILABLE_POLL_INTERVAL)
                continue
            except platform.CursorError as exc:
                if interactive:
                    print()
                ui.error(str(exc))
                return 1
            waiting = False
            marker = ""
            if trigger is not None:
                marker = "  <- inside region" if trigger.contains((x, y)) else "                  "
            line = f"x={x:<6d} y={y:<6d}{marker}"
            print(f"\r{line}" if interactive else line, end="" if interactive else "\n", flush=True)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        if interactive:
            print()
        ui.info("Stopped.")
        return 0


def run_ask(cfg: Mapping[str, Any], question: str | None) -> int:
    """Capture once, then answer questions about that screenshot (FR-14).

    Returns a non-zero exit code if any turn failed, so that
    ``screenrecon ask "..." && do_something`` does not run on an API error.
    """
    check_environment(cfg)
    print_destinations(cfg)
    try:
        image = capture.grab(cfg["region"])
        _warn_if_blank(image)
        archive_png, api_png = _encode(image)
    except capture.CaptureError as exc:
        ui.error(str(exc))
        return 1

    directory = None
    stem = ""
    saved_png = None
    try:
        directory = storage.resolve_dir(str(cfg["save_dir"]))
        stem = storage.new_stem(directory)
        saved_png = storage.save_png(directory, stem, archive_png)
        if saved_png is not None:
            ui.info(f"Captured and saved to {saved_png}")
    except (OSError, RuntimeError) as exc:
        ui.warn(f"Save directory unusable ({cfg['save_dir']}): {exc}")

    api_key = str(cfg["anthropic_api_key"])
    model = str(cfg["model"])
    one_shot = bool(question)
    messages: list[dict[str, Any]] = []
    transcript: list[str] = []
    first = True
    failed = False

    while True:
        if question:
            current = question
            question = None  # the command-line question is used once
        else:
            try:
                current = input("\nYour question (Enter to finish): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not current:
                break

        messages.append(vision.user_turn(api_png if first else None, current))
        first = False
        reply = vision.ask(api_key, model, messages)

        if reply.ok:
            ui.rule("Answer")
            ui.info(reply.text)
            transcript.append(f"Q: {current}\nA: {reply.text}")
        else:
            ui.error(reply.text)
            failed = True
            messages.pop()  # a failed turn does not enter the conversation history
            break
        if one_shot:
            break
        messages.append({"role": "assistant", "content": [{"type": "text", "text": reply.text}]})

    if transcript and saved_png is not None and directory is not None and stem:
        storage.save_txt(directory, stem, "\n\n".join(transcript))
    return 1 if failed else 0
