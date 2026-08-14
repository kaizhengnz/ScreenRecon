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

MODEL_CHOICES: list[tuple[str, str, str]] = [
    ("claude-opus-5", "claude-opus-5", "default, high accuracy"),
    ("claude-haiku-4-5", "claude-haiku-4-5", "cheaper, faster"),
]
"""Built-in model choices for the wizard: ``(display_label, stored_value, note)``.

For models the label equals the value (the model ID is short and readable), so
the list looks a bit redundant. The three-tuple shape is the same one
:data:`PROMPT_CHOICES` uses, where the label and the value differ.

The wizard tacks the current value on as the final numbered option (index
``len(MODEL_CHOICES) + 1``) so pressing Enter always maps to "keep current".
"""

PROMPT_CHOICES: list[tuple[str, str, str]] = [
    (
        "describe",
        "Describe what is in this screenshot. Be concise and lead with the key information.",
        "recognition or OCR",
    ),
    (
        "answer",
        "Read the question in this screenshot and answer it.",
        "answer questions in the image",
    ),
]
"""Built-in prompt presets. Same ``(label, value, note)`` shape as :data:`MODEL_CHOICES`."""

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


def _ask_choice(
    label: str,
    presets: list[tuple[str, str, str]],
    current: str,
) -> str:
    """Present ``presets`` as numbered options 1..N, with ``current`` as option N+1.

    Each preset is ``(label, value, note)``: the label is what the user sees in
    the numbered list, the value is what is stored / returned, the note goes in
    parentheses next to the label. For a model whose ID is short and readable
    the label equals the value; for a prompt whose text is long the label is a
    short synonym (e.g. ``"describe"`` for a full sentence prompt).

    Behaviour:

    - Input a number in ``1..N+1`` → returns that option's value.
    - Input any non-empty non-numeric text → returned as a custom value (so
      users can pin a value the wizard does not know about yet).
    - Enter → keeps ``current`` (the prompt hint is ``[N+1]``; empty answer
      resolves to that index and therefore to the current value).

    Number out of range warns and re-prompts, up to ``MAX_PROMPT_RETRIES``.
    """
    current_index = len(presets) + 1
    labels = [preset_label for preset_label, _, _ in presets]
    width = max(len(preset_label) for preset_label in labels) if labels else 0
    current_preview = current if len(current) <= 60 else current[:57] + "..."

    for index, (preset_label, _, note) in enumerate(presets, start=1):
        ui.info(f"    {index}) {preset_label:<{width}}  ({note})")
    ui.info(f"    {current_index}) (keep current — {current_preview})")

    for _ in range(MAX_PROMPT_RETRIES):
        answer = _ask(
            f"    Enter 1-{current_index} or type any {label}",
            str(current_index),
        ).strip()
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(presets):
                return presets[number - 1][1]
            if number == current_index:
                return current
            ui.warn(f"    Choice must be 1-{current_index}, please try again.")
            continue
        return answer  # custom value
    raise WizardAborted(f"{label}: too many invalid answers, giving up.")


def _prompt_region(
    current: dict[str, Any],
    picker_factory: PickerFactory | None,
) -> dict[str, Any]:
    """Print the current region, offer to update it, and delegate to the picker.

    Kept intentionally thin — the picker owns the whole "give me a region"
    concern (open, cancel-fallback, PickerError-fallback, monitor reporting).
    """
    from . import display, picker as picker_module

    ui.info(
        f"   Current: left={current.get('left')} top={current.get('top')} "
        f"width={current.get('width')} height={current.get('height')}"
        + display.describe_region_monitor(current)
    )
    answer = _ask("   Update this region? [Y/n]", "Y").strip().lower()
    if answer in ("n", "no"):
        return current
    return picker_module.pick_region_or_default(current, picker_factory)


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
    from . import notify, picker as picker_module, vision  # lazy: --help skips SDK

    resolved = config_path(path)
    raw = read_raw(resolved)
    cfg = merge_defaults(raw)

    # First-time (and partial-config) users have no complete saved region, and
    # the merge fills in the hardcoded 100/100/600/400 DEFAULTS for the missing
    # keys. Replace that with a live centered region so the "Current" line
    # reflects something sensible (and, if they answer "n" to the picker, that
    # centered region is what gets saved). If the cursor cannot be read at all,
    # keep the merged DEFAULTS so the wizard still starts.
    saved_region = raw.get("region")
    has_complete_region = isinstance(saved_region, dict) and all(
        k in saved_region for k in ("left", "top", "width", "height")
    )
    if not has_complete_region:
        centered = picker_module.default_centered_region_or_none()
        if centered is not None:
            cfg["region"] = centered

    ui.rule("ScreenRecon setup")
    ui.info(f"Config file: {resolved}")
    ui.info("Press Enter to keep the current value. Press Ctrl+C at any time to abort — nothing is saved until every step is done.\n")

    ui.info("1) Watched region")
    cfg["region"] = _prompt_region(dict(cfg["region"]), picker_factory)

    ui.info("\n2) Trigger")
    ui.info(
        "   A capture fires when the cursor stays in the region for this many"
        " seconds. After a capture, the cursor must leave and re-enter the"
        " region to arm the next one."
    )
    cfg["dwell_seconds"] = _ask_float("  dwell seconds", cfg["dwell_seconds"], minimum=0)

    ui.info("\n3) AI model")
    cfg["model"] = _ask_choice("AI model", MODEL_CHOICES, str(cfg["model"]))

    ui.info("\n4) Default prompt")
    cfg["prompt"] = _ask_choice("default prompt", PROMPT_CHOICES, str(cfg["prompt"]))

    ui.info("\n5) Credentials (never echoed in full)")
    env_key = os.environ.get(ENV_API_KEY, "").strip()
    if env_key:
        ui.info(f"  {ENV_API_KEY} is set ({ui.mask(env_key)}); it wins over this file at runtime.")
    cfg["anthropic_api_key"] = _ask("  Anthropic API key", cfg["anthropic_api_key"], secret=True)
    cfg["telegram_bot_token"] = _ask("  Telegram bot token", cfg["telegram_bot_token"], secret=True)
    cfg["telegram_chat_id"] = _ask("  Telegram chat ID", cfg["telegram_chat_id"], secret=True)

    ui.info("\n6) Local archive")
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
