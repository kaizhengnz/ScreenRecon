"""Config loading, validation, saving and the interactive wizard (design doc 5.7)."""

from __future__ import annotations

import getpass
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import storage, ui

if TYPE_CHECKING:
    from . import picker as picker_module

    PickerFactory = Callable[[], picker_module.RegionPicker]
else:
    PickerFactory = Callable[[], Any]


def _default_config_path() -> Path:
    """``$XDG_CONFIG_HOME/screenrecon/config.json``, else ``~/.config/...``."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "screenrecon" / "config.json"


def __getattr__(name: str) -> Any:
    """Resolve DEFAULT_CONFIG_PATH lazily (PEP 562).

    Computing it at import time would raise RuntimeError before any handler
    exists on a system with no home directory (a bare container, a systemd unit
    with PrivateUsers), producing a raw traceback.
    """
    if name == "DEFAULT_CONFIG_PATH":
        return _default_config_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

DEFAULT_PROMPT = (
    "Describe what is in this screenshot. Be concise and lead with the key information."
)
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_SAVE_DIR = "~/ScreenRecon"

ENV_API_KEY = "ANTHROPIC_API_KEY"

DEFAULTS: dict[str, Any] = {
    "region": {"left": 100, "top": 100, "width": 600, "height": 400},
    "anthropic_api_key": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "save_dir": DEFAULT_SAVE_DIR,
    "prompt": DEFAULT_PROMPT,
    "prompts": {},
    "dwell_seconds": 3,
    "model": DEFAULT_MODEL,
}

CREDENTIAL_FIELDS = ("anthropic_api_key", "telegram_bot_token", "telegram_chat_id")

_CREDENTIAL_LABELS = {
    "anthropic_api_key": "Anthropic API key",
    "telegram_bot_token": "Telegram bot token",
    "telegram_chat_id": "Telegram chat ID",
}


class ConfigError(Exception):
    """Config is missing or invalid. The message is safe to show the user."""


class WizardAborted(Exception):
    """The setup wizard cannot continue (no input available, or too many retries)."""


# --------------------------------------------------------------------------- #
# Load / save
# --------------------------------------------------------------------------- #


def config_path(override: str | os.PathLike[str] | None = None) -> Path:
    """Return the config file path actually in use (see --config, design doc 5.8)."""
    if override:
        return Path(override).expanduser()
    # Re-read rather than using the module constant, so XDG_CONFIG_HOME set after
    # import (and tests that patch it) are honoured.
    return _default_config_path()


def read_raw(path: Path) -> dict[str, Any]:
    """Read the raw config file. A missing file yields an empty dict."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc.strerror or exc}") from None
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Config file {path} is not valid JSON (line {exc.lineno}): {exc.msg}. "
            "Run 'screenrecon --configure' to regenerate it."
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(f"The top level of config file {path} must be a JSON object.")
    return data


def merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge with defaults; 'region' and 'prompts' are merged one level deep."""
    merged: dict[str, Any] = dict(DEFAULTS)
    merged["region"] = dict(DEFAULTS["region"])
    merged["prompts"] = {}

    for key, value in raw.items():
        if key == "region" and isinstance(value, dict):
            merged["region"].update(value)
        elif key == "prompts" and isinstance(value, dict):
            merged["prompts"] = dict(value)
        else:
            merged[key] = value
    return merged


def apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """ANTHROPIC_API_KEY takes precedence over the key in the file (FR-15)."""
    env_key = os.environ.get(ENV_API_KEY, "").strip()
    if env_key:
        cfg["anthropic_api_key"] = env_key
    return cfg


def load(path: str | os.PathLike[str] | None = None, *, validate: bool = True) -> dict[str, Any]:
    """Load config: merge defaults, apply env overrides, then validate."""
    resolved = config_path(path)
    cfg = apply_env_overrides(merge_defaults(read_raw(resolved)))
    cfg["_path"] = str(resolved)
    if validate:
        validate_config(cfg)
    return cfg


def save(cfg: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    """Write the config file with owner-only permissions (NFR-4).

    The file is created 0600 and moved into place atomically, so the credentials
    are never briefly readable at the process umask, a crash cannot leave a torn
    config behind, and a symlink planted at the destination is replaced rather
    than followed.
    """
    resolved = config_path(path or cfg.get("_path"))
    payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        storage.make_private_dir(resolved.parent)
        storage.write_private_text(resolved, text)
    except OSError as exc:
        raise ConfigError(f"Cannot write config file {resolved}: {exc.strerror or exc}") from None
    return resolved


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"Config field '{field}' must be an integer, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise ConfigError(f"Config field '{field}' must be an integer, got {value!r}")
    return int(value)


def validate_config(cfg: dict[str, Any]) -> None:
    """Validate non-credential fields; invalid values raise a field-specific error."""
    region = cfg.get("region")
    if not isinstance(region, dict):
        raise ConfigError(
            "Config field 'region' must be an object, e.g. "
            '{"left": 100, "top": 100, "width": 600, "height": 400}'
        )
    for field in ("left", "top", "width", "height"):
        if field not in region:
            raise ConfigError(
                f"Config field 'region.{field}' is missing. Run 'screenrecon --configure'."
            )
    # left/top may be negative: secondary monitors can sit at negative coordinates.
    _as_int(region["left"], "region.left")
    _as_int(region["top"], "region.top")
    for field in ("width", "height"):
        value = _as_int(region[field], f"region.{field}")
        if value <= 0:
            raise ConfigError(
                f"Config field 'region.{field}' must be a positive integer, got {region[field]!r}"
            )

    dwell = cfg.get("dwell_seconds")
    if isinstance(dwell, bool) or not isinstance(dwell, int | float):
        raise ConfigError(f"Config field 'dwell_seconds' must be a number, got {dwell!r}")
    if dwell <= 0:
        raise ConfigError(f"Config field 'dwell_seconds' must be greater than 0, got {dwell!r}")

    for field in ("model", "prompt", "save_dir"):
        value = cfg.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"Config field '{field}' must be a non-empty string, got {value!r}")

    prompts = cfg.get("prompts")
    if not isinstance(prompts, dict):
        raise ConfigError('Config field \'prompts\' must be an object, e.g. {"log": "Find errors"}')
    for name, value in prompts.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"Config field 'prompts.{name}' must be a non-empty string")

    for field in CREDENTIAL_FIELDS:
        value = cfg.get(field)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"Config field '{field}' must be a string")


def require_credentials(cfg: dict[str, Any]) -> None:
    """Refuse to start when any of the three credentials is empty (design doc 5.7)."""
    missing = [
        _CREDENTIAL_LABELS[field]
        for field in CREDENTIAL_FIELDS
        if not str(cfg.get(field) or "").strip()
    ]
    if missing:
        raise ConfigError(
            "Not configured yet: " + ", ".join(missing) + ".\n"
            "Run 'screenrecon --configure' to set them."
        )


def resolve_prompt(cfg: dict[str, Any], mode: str | None) -> str:
    """Resolve --mode to a prompt preset (FR-13); falls back to the default prompt."""
    if mode is None:
        return str(cfg["prompt"])
    prompts: dict[str, str] = cfg.get("prompts") or {}
    if mode in prompts:
        return str(prompts[mode])
    if mode == "default":
        return str(cfg["prompt"])
    available = ", ".join(sorted(prompts)) or "(no presets defined in config)"
    raise ConfigError(f"No prompt preset named {mode!r}. Available presets: {available}")


# --------------------------------------------------------------------------- #
# Interactive wizard (FR-11)
# --------------------------------------------------------------------------- #


MAX_PROMPT_RETRIES = 5
"""Give up after this many unusable answers, rather than looping forever."""


def _ask(label: str, current: Any, *, secret: bool = False) -> str:
    """Prompt for one value. Enter keeps the current value.

    Secrets are read with getpass so the typed value is never echoed to the
    terminal, never reaches shell/readline history, and cannot be captured from
    scrollback — or by ScreenRecon's own screenshots (NFR-3).
    """
    has_current = current is not None and str(current) != ""
    if not has_current:
        hint = "empty"
    elif secret:
        hint = ui.mask(str(current))
    else:
        hint = str(current)

    prompt = f"{label} [{hint}]: "
    try:
        answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except EOFError:
        raise WizardAborted(
            "No input available (stdin is closed). Run 'screenrecon --configure' "
            "from an interactive terminal."
        ) from None
    # An empty answer means "keep the current value" — including a current 0.
    if answer:
        return answer
    return "" if current is None else str(current)


def _ask_int(label: str, current: Any, *, minimum: int | None = None) -> int:
    for _ in range(MAX_PROMPT_RETRIES):
        answer = _ask(label, current)
        try:
            value = int(str(answer).strip())
        except ValueError:
            ui.warn(f"{label} needs an integer, please try again.")
            continue
        if minimum is not None and value < minimum:
            ui.warn(f"{label} must be at least {minimum}, please try again.")
            continue
        return value
    raise WizardAborted(f"{label}: too many invalid answers, giving up.")


def _ask_float(label: str, current: Any, *, minimum: float | None = None) -> float:
    for _ in range(MAX_PROMPT_RETRIES):
        answer = _ask(label, current)
        try:
            value = float(str(answer).strip())
        except ValueError:
            ui.warn(f"{label} needs a number, please try again.")
            continue
        if minimum is not None and value <= minimum:
            ui.warn(f"{label} must be greater than {minimum}, please try again.")
            continue
        return value
    raise WizardAborted(f"{label}: too many invalid answers, giving up.")


def _pick_region_step(
    current: dict[str, Any],
    picker_factory: PickerFactory | None,
) -> dict[str, Any]:
    """Update the watched region via the interactive picker, or keep the current one.

    Behaviour:
    - Prompt "Update this region? [Y/n]". Answer 'n' keeps the current value.
    - Otherwise open the picker. A successful drag becomes the new region.
    - If the picker returns nothing (Esc, close, or zero-area click), fall back
      to a ``DEFAULT_PICKED_WIDTH × DEFAULT_PICKED_HEIGHT`` region centered on
      the monitor that currently holds the cursor.
    - If the picker cannot open at all (no display, missing tkinter), warn and
      keep the current region.
    """
    from . import picker as picker_module
    from . import platform as cursor_platform

    ui.info(
        f"   Current: left={current.get('left')} top={current.get('top')} "
        f"width={current.get('width')} height={current.get('height')}"
    )
    answer = _ask("   Update this region? [Y/n]", "Y").strip().lower()
    if answer in ("n", "no"):
        return current

    factory = picker_factory or picker_module.default_picker
    ui.info(
        "   Opening the picker. Drag a rectangle, or press Esc to use "
        f"{picker_module.DEFAULT_WIDTH}x{picker_module.DEFAULT_HEIGHT} "
        "centered on the current monitor."
    )
    try:
        picked = factory().pick()
    except picker_module.PickerError as exc:
        ui.warn(f"   {exc}")
        ui.info("   Keeping the current region.")
        return current

    if picked is not None:
        _report_picked_region(picked, cursor_platform)
        return picked

    # No drag — apply the default centered on whichever monitor the cursor is on.
    try:
        cursor_platform.ensure_reader()
        x, y = cursor_platform.get_cursor_pos()
    except (cursor_platform.CursorError, cursor_platform.CursorUnavailable) as exc:
        ui.warn(f"   Could not read cursor position for the default region: {exc}")
        ui.info("   Keeping the current region.")
        return current

    region = picker_module.default_region_at(x, y)
    ui.info(
        f"   Using default: left={region['left']} top={region['top']} "
        f"width={region['width']} height={region['height']}"
    )
    return region


def _report_picked_region(region: dict[str, Any], cursor_platform: Any) -> None:
    """Print the picked region and, when known, which monitor it landed on."""
    monitors = cursor_platform.enumerate_monitors()
    line = (
        f"   Picked: left={region['left']} top={region['top']} "
        f"width={region['width']} height={region['height']}"
    )
    if monitors:
        cx = region["left"] + region["width"] // 2
        cy = region["top"] + region["height"] // 2
        for index, mon in enumerate(monitors, start=1):
            if (
                mon["left"] <= cx < mon["left"] + mon["width"]
                and mon["top"] <= cy < mon["top"] + mon["height"]
            ):
                line += f" (on monitor {index} of {len(monitors)})"
                break
    ui.info(line)


def run_wizard(
    path: str | os.PathLike[str] | None = None,
    *,
    picker_factory: PickerFactory | None = None,
) -> int:
    """Ask for each field (Enter keeps the current value), then verify credentials online.

    ``picker_factory`` is a seam for tests: pass a callable returning a
    :class:`picker.ScriptedPicker` (or any :class:`picker.RegionPicker`). Production
    leaves it ``None`` and the real :class:`picker.TkDragPicker` is used.
    """
    try:
        return _run_wizard(path, picker_factory)
    except WizardAborted as exc:
        ui.error(str(exc))
        ui.error("Nothing was saved.")
        return 1
    except KeyboardInterrupt:
        print()
        ui.info("Setup cancelled. Nothing was saved.")
        return 130
    except Exception as exc:
        # The credentials the user just typed are live in this call stack, and
        # they are not available to the handler in cli.main. Report the type
        # only — never the message, which could quote them.
        ui.error(f"Setup failed: {type(exc).__name__}")
        ui.error("Nothing was saved. Run 'screenrecon --configure' again.")
        return 1


def _run_wizard(
    path: str | os.PathLike[str] | None = None,
    picker_factory: PickerFactory | None = None,
) -> int:
    from . import notify, vision  # imported lazily so --help never loads the SDK

    resolved = config_path(path)
    cfg = merge_defaults(read_raw(resolved))

    ui.rule("ScreenRecon setup")
    ui.info(f"Config file: {resolved}")
    ui.info("Press Enter to keep the current value.\n")

    ui.info("1) Watched region")
    cfg["region"] = _pick_region_step(dict(cfg["region"]), picker_factory)

    ui.info("\n2) Trigger and model")
    cfg["dwell_seconds"] = _ask_float("  dwell seconds", cfg["dwell_seconds"], minimum=0)
    cfg["model"] = _ask("  AI model", cfg["model"])
    cfg["prompt"] = _ask("  default prompt", cfg["prompt"])

    ui.info("\n3) Credentials (never echoed in full)")
    env_key = os.environ.get(ENV_API_KEY, "").strip()
    if env_key:
        ui.info(f"  {ENV_API_KEY} is set ({ui.mask(env_key)}); it wins over this file at runtime.")
    cfg["anthropic_api_key"] = _ask("  Anthropic API key", cfg["anthropic_api_key"], secret=True)
    cfg["telegram_bot_token"] = _ask("  Telegram bot token", cfg["telegram_bot_token"], secret=True)
    cfg["telegram_chat_id"] = _ask("  Telegram chat ID", cfg["telegram_chat_id"], secret=True)

    ui.info("\n4) Local archive")
    cfg["save_dir"] = _ask("  save directory", cfg["save_dir"])

    ui.rule("Verifying credentials")
    ok_claude, msg_claude = vision.verify_key(str(cfg["anthropic_api_key"]), str(cfg["model"]))
    ui.info(("  OK   " if ok_claude else "  FAIL ") + f"Anthropic: {msg_claude}")
    ok_tg, msg_tg = notify.verify_credentials(
        str(cfg["telegram_bot_token"]), str(cfg["telegram_chat_id"])
    )
    ui.info(("  OK   " if ok_tg else "  FAIL ") + f"Telegram: {msg_tg}")

    try:
        validate_config(merge_defaults(cfg))
    except ConfigError as exc:
        ui.error(str(exc))
        ui.error("Nothing was saved. Run 'screenrecon --configure' again.")
        return 1

    saved_to = save(cfg, resolved)
    ui.rule()
    ui.info(f"Config saved to {saved_to}")
    if not (ok_claude and ok_tg):
        ui.warn("Some credentials failed verification. The config was saved anyway.")
    return 0
