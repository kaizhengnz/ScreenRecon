"""Config loading, validation, saving and the interactive wizard (design doc 5.7)."""

from __future__ import annotations

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
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_SAVE_DIR = "~/ScreenRecon"

MODEL_CHOICES_BY_PROVIDER: dict[str, list[tuple[str, str, str]]] = {
    "anthropic": [
        ("claude-opus-5", "claude-opus-5", "high accuracy, more expensive"),
        ("claude-haiku-4-5", "claude-haiku-4-5", "cheaper and faster — the default"),
    ],
    "openai": [
        ("gpt-5", "gpt-5", "top OpenAI vision"),
        ("gpt-5-mini", "gpt-5-mini", "cheaper and faster"),
    ],
    "google": [
        ("gemini-2.5-pro", "gemini-2.5-pro", "high accuracy"),
        ("gemini-2.5-flash", "gemini-2.5-flash", "fast and cheap"),
    ],
    "openai_compatible": [
        # Model names for compat endpoints are provider-specific; presets
        # below carry a suggested default per endpoint. The wizard tacks
        # "type any model ID" onto the choice, so custom entries are trivial.
    ],
}
"""Curated per-provider model shortlists for the wizard. Not exhaustive —
the wizard always accepts a typed custom model ID via ``_ask_choice``.
Ordering: the recommended default is second (index 2) so a fresh install
lands on the cheaper/faster option; expensive picks sit at index 1 for
users who type-ahead their preference."""


COMPAT_PRESETS: dict[str, tuple[str, str, str]] = {
    # label -> (base_url, default_model, note)
    "deepseek": (
        "https://api.deepseek.com/v1",
        "deepseek-vl2",
        "DeepSeek official — vision-capable",
    ),
    "kimi": (
        "https://api.moonshot.cn/v1",
        "moonshot-v1-8k-vision-preview",
        "Moonshot / Kimi — vision preview",
    ),
    "doubao": (
        "https://ark.cn-beijing.volces.com/api/v3",
        "doubao-1-5-vision-pro-32k-241015",
        "Doubao (ByteDance / Volcano Engine)",
    ),
}
"""Shortcuts for the OpenAI-compatible provider. The wizard asks for a
preset label or a custom base URL; when a label is picked, the default
model is pre-filled but still editable. Endpoints verified against each
provider's compat-mode docs at the time of writing (2026-08)."""

DEFAULTS: dict[str, Any] = {
    "region": {"left": 100, "top": 100, "width": 600, "height": 400},
    # provider: empty means "infer from model prefix"; the dispatcher falls
    # back to anthropic for unknown prefixes so existing configs keep working.
    "provider": "",
    "api_key": "",
    # base_url: only meaningful when provider == "openai_compatible".
    "base_url": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "save_dir": DEFAULT_SAVE_DIR,
    "prompt": DEFAULT_PROMPT,
    "dwell_seconds": 3,
    "model": DEFAULT_MODEL,
}

LEGACY_PROMPTS_FIELD = "prompts"
"""Pre-SR-36 configs stored named prompt presets here for --mode NAME. Both
are gone — merge_defaults drops the field on read, and save omits it on the
next write. Kept as a constant so a search finds the migration site."""

CREDENTIAL_FIELDS = ("api_key", "telegram_bot_token", "telegram_chat_id")

_CREDENTIAL_LABELS = {
    "api_key": "AI API key",
    "telegram_bot_token": "Telegram bot token",
    "telegram_chat_id": "Telegram chat ID",
}

LEGACY_API_KEY_FIELD = "anthropic_api_key"
"""Pre-SR-23 configs stored the Anthropic key here. ``merge_defaults`` migrates
into ``api_key`` on read; ``save`` strips the old field on write. Kept as a
constant rather than a string literal so a search finds every reference at
once when the migration window ends."""


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
    """Merge with defaults; ``region`` is merged one level deep.

    Also migrates the pre-SR-23 ``anthropic_api_key`` field into ``api_key``
    when the new field is absent, so a config from 0.1.5 keeps working
    unchanged until the user runs any setter (which then rewrites without
    the legacy field). This migration happens in memory only — the file
    on disk is untouched until the next explicit save. The pre-SR-36
    ``prompts`` dict is dropped the same way.
    """
    merged: dict[str, Any] = dict(DEFAULTS)
    merged["region"] = dict(DEFAULTS["region"])

    for key, value in raw.items():
        if key == LEGACY_PROMPTS_FIELD:
            continue
        if key == "region" and isinstance(value, dict):
            merged["region"].update(value)
        else:
            merged[key] = value

    # Legacy migration: fall back to anthropic_api_key when api_key is empty.
    if not str(merged.get("api_key") or "").strip():
        legacy = str(raw.get(LEGACY_API_KEY_FIELD) or "").strip()
        if legacy:
            merged["api_key"] = legacy
    return merged


def load(path: str | os.PathLike[str] | None = None, *, validate: bool = True) -> dict[str, Any]:
    """Load config: merge defaults, then validate.

    No environment-variable overrides — SR-23 dropped `ANTHROPIC_API_KEY` in
    favour of a single-source-of-truth config file. Users who relied on the
    env variable move the key into the file with ``screenrecon --key``.
    """
    resolved = config_path(path)
    cfg = merge_defaults(read_raw(resolved))
    cfg["_path"] = str(resolved)
    if validate:
        validate_config(cfg)
    return cfg


def save(cfg: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    """Write the config file with owner-only permissions (NFR-4).

    The file is created 0600 and moved into place atomically, so the credentials
    are never briefly readable at the process umask, a crash cannot leave a torn
    config behind, and a symlink planted at the destination is replaced rather
    than followed. Any legacy ``anthropic_api_key`` field is dropped from the
    payload once ``api_key`` is populated — the migration is one-way and
    completes at the next save.
    """
    resolved = config_path(path or cfg.get("_path"))
    payload: dict[str, Any] = {}
    for key, value in cfg.items():
        if key.startswith("_"):
            continue
        # Drop legacy fields on write so old configs converge on the new schema.
        if key == LEGACY_API_KEY_FIELD and str(cfg.get("api_key") or "").strip():
            continue
        if key == LEGACY_PROMPTS_FIELD:
            continue
        payload[key] = value
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

    for field in CREDENTIAL_FIELDS:
        value = cfg.get(field)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"Config field '{field}' must be a string")

    provider = cfg.get("provider")
    if provider is not None and not isinstance(provider, str):
        raise ConfigError(f"Config field 'provider' must be a string, got {provider!r}")
    if isinstance(provider, str) and provider.strip():
        # Lazy import so this module stays test-importable without the SDK.
        from . import vision as _vision

        if provider not in {p.name for p in _vision.known_providers()}:
            raise ConfigError(
                f"Unknown provider {provider!r} in config. "
                f"Known: {', '.join(sorted(p.name for p in _vision.known_providers()))}."
            )
        if provider == "openai_compatible" and not str(cfg.get("base_url") or "").strip():
            raise ConfigError(
                "Config field 'base_url' is required when 'provider' is "
                "'openai_compatible'. Run 'screenrecon --model' to set it."
            )

    base_url = cfg.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise ConfigError(f"Config field 'base_url' must be a string, got {base_url!r}")


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


# --------------------------------------------------------------------------- #
# Interactive wizard (FR-11)
# --------------------------------------------------------------------------- #


MAX_PROMPT_RETRIES = 5
"""Give up after this many unusable answers, rather than looping forever."""


def _ask(label: str, current: Any, *, secret: bool = False) -> str:
    """Prompt for one value. Enter keeps the current value.

    ``secret=True`` no longer hides characters during typing — API keys, bot
    tokens and chat IDs are all printable ASCII, and hiding them broke paste
    on Windows and left the user unsure whether their input landed. What
    ``secret`` still does: shows the current value as a mask in the ``[hint]``
    (so an existing key is not re-displayed in full), and echoes a masked
    confirmation after Enter (so scrollback keeps only ``sk-ant-a... (N chars)``
    prefixes instead of the whole key repeated). NFR-3 still applies at
    runtime — the watch loop, logs, and tracebacks never quote a secret.
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
        answer = input(prompt).strip()
    except EOFError:
        raise WizardAborted(
            "No input available (stdin is closed). Run 'screenrecon --configure' "
            "from an interactive terminal."
        ) from None
    # An empty answer means "keep the current value" — including a current 0.
    if answer:
        if secret:
            ui.info(f"    received: {ui.mask(answer)}")
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
    *,
    default: str | None = None,
) -> str:
    """Present ``presets`` as numbered options 1..N, with ``current`` as option N+1.

    Each preset is ``(label, value, note)``: the label is what the user sees in
    the numbered list, the value is what is stored / returned, the note goes in
    parentheses next to the label. For a model whose ID is short and readable
    the label equals the value; for a prompt whose text is long the label is a
    short synonym (e.g. ``"describe"`` for a full sentence prompt).

    Enter maps to whichever index the prompt hint shows: by default that is
    ``N+1`` (keep current), so a user who runs the wizard on an existing config
    is not surprised. Pass ``default`` (a value that must match one of the
    preset values) to make Enter select a specific recommended preset instead
    — used for the AI model, where the shipping recommendation should be what
    a quick Enter lands on.

    Number out of range warns and re-prompts, up to ``MAX_PROMPT_RETRIES``.
    Non-numeric text is returned verbatim as a custom value.
    """
    current_index = len(presets) + 1
    labels = [preset_label for preset_label, _, _ in presets]
    width = max(len(preset_label) for preset_label in labels) if labels else 0
    current_preview = current if len(current) <= 60 else current[:57] + "..."

    default_index = current_index
    if default is not None:
        for index, (_, preset_value, _) in enumerate(presets, start=1):
            if preset_value == default:
                default_index = index
                break

    for index, (preset_label, _, note) in enumerate(presets, start=1):
        suffix = f"  ({note})" if note else ""
        ui.info(f"    {index}) {preset_label:<{width}}{suffix}".rstrip())
    ui.info(f"    {current_index}) (keep current — {current_preview})")

    for _ in range(MAX_PROMPT_RETRIES):
        answer = _ask(
            f"    Enter 1-{current_index} or type any {label}",
            str(default_index),
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
    current_monitor: dict[str, int] | None,
    picker_factory: PickerFactory | None,
) -> dict[str, Any]:
    """Print the current region, offer to update it, and delegate to the picker.

    Kept intentionally thin — the picker owns the whole "give me a region"
    concern (open, cancel-fallback, PickerError-fallback, monitor reporting).
    ``current_monitor`` is the config's stored ``monitor`` field (if any);
    the "Current" line uses it verbatim rather than recomputing, so what the
    user sees matches what the config records.
    """
    from . import display
    from . import picker as picker_module

    annotation = (
        display.format_monitor_info(current_monitor)
        or display.describe_region_monitor(current)
    )
    ui.info(
        f"   Current: left={current.get('left')} top={current.get('top')} "
        f"width={current.get('width')} height={current.get('height')}"
        + annotation
    )
    answer = _ask("   Update this region?", "N").strip().lower()
    if answer in ("y", "yes"):
        return picker_module.pick_region_or_default(current, picker_factory)
    return current


def _apply_monitor_info(
    target: dict[str, Any], region: dict[str, Any]
) -> None:
    """Compute the monitor for ``region`` right now and write / clear
    ``target["monitor"]`` accordingly.

    The stored value is what the user chose — it does not drift as monitors
    are plugged / unplugged or as `mss` reports them differently across DPI
    contexts (the SR-20 class of confusion). Clearing on failure prevents a
    stale annotation from lingering when the enumeration cannot be trusted.
    """
    from . import display

    info = display.resolve_monitor_info(region)
    if info is not None:
        target["monitor"] = info
    elif "monitor" in target:
        del target["monitor"]


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


def run_set_region(
    path: str | os.PathLike[str] | None = None,
    *,
    picker_factory: PickerFactory | None = None,
) -> int:
    """Re-pick just the watched region; leave every other config field alone.

    For the "I moved monitors / changed scaling / want to reframe" case, where
    the full wizard (credentials verification, prompt/model choice, etc.) is
    overkill. Refuses to run if no config exists yet — the user needs to have
    gone through `--configure` at least once so credentials are present.
    """
    from . import display
    from . import picker as picker_module

    resolved = config_path(path)
    raw = read_raw(resolved)
    if not raw:
        ui.error(
            f"No config to update at {resolved}. "
            "Run 'screenrecon --configure' first to set credentials and save directory."
        )
        return 1

    cfg = merge_defaults(raw)
    ui.rule("ScreenRecon set region")
    ui.info(f"Config file: {resolved}")
    stored_monitor = raw.get("monitor") if isinstance(raw.get("monitor"), dict) else None
    current_annotation = (
        display.format_monitor_info(stored_monitor)
        or display.describe_region_monitor(cfg["region"])
    )
    ui.info(
        f"   Current: left={cfg['region'].get('left')} top={cfg['region'].get('top')} "
        f"width={cfg['region'].get('width')} height={cfg['region'].get('height')}"
        + current_annotation
    )
    new_region = picker_module.pick_region_or_default(
        dict(cfg["region"]), picker_factory
    )
    raw["region"] = new_region
    _apply_monitor_info(raw, new_region)

    try:
        validate_config(merge_defaults(raw))
    except ConfigError as exc:
        ui.error(str(exc))
        return 1

    try:
        saved_to = save(raw, resolved)
    except ConfigError as exc:
        ui.error(str(exc))
        return 1
    ui.rule()
    ui.info(f"Region saved to {saved_to}")
    return 0


def _run_single_field_setter(
    path: str | os.PathLike[str] | None,
    banner: str,
    setter: Callable[[dict[str, Any]], None],
) -> int:
    """Shared skeleton for ``--key`` / ``--model``: load, mutate one field, save.

    Refuses to run without an existing config so credentials are always set
    via ``--configure`` first — mirrors ``run_set_region``. ``setter`` mutates
    the raw dict in place; on ``WizardAborted`` / ``KeyboardInterrupt`` the
    file is left untouched.
    """
    resolved = config_path(path)
    raw = read_raw(resolved)
    if not raw:
        ui.error(
            f"No config to update at {resolved}. "
            "Run 'screenrecon --configure' first to set credentials and save directory."
        )
        return 1

    ui.rule(banner)
    ui.info(f"Config file: {resolved}")
    try:
        setter(raw)
    except WizardAborted as exc:
        ui.error(str(exc))
        ui.error("Nothing was saved.")
        return 1
    except KeyboardInterrupt:
        print()
        ui.info("Cancelled. Nothing was saved.")
        return 130
    except ConfigError as exc:
        # A setter that validates up-front (e.g. run_set_key) surfaces a
        # broken existing config here before the user has typed anything.
        ui.error(str(exc))
        ui.error("Nothing was saved.")
        return 1

    try:
        validate_config(merge_defaults(raw))
    except ConfigError as exc:
        ui.error(str(exc))
        return 1

    try:
        saved_to = save(raw, resolved)
    except ConfigError as exc:
        ui.error(str(exc))
        return 1
    ui.rule()
    ui.info(f"Saved to {saved_to}")
    return 0


def run_set_key(path: str | os.PathLike[str] | None = None) -> int:
    """Prompt for a new API key for whichever provider the current config uses.

    ``--key`` is provider-agnostic: it writes to ``api_key``, which the
    dispatcher hands to whichever provider ``cfg["provider"]`` (or the model
    prefix) selects. A user who just switched providers via ``--model`` runs
    ``--key`` next without having to remember which provider they picked.
    """
    from . import vision as _vision

    def setter(raw: dict[str, Any]) -> None:
        # Pass a fresh merged view so provider inference sees the migrated
        # api_key; the write below still goes to `raw`. Translate the
        # dispatcher's KeyError on an unknown provider name into a
        # ConfigError so hand-edited configs get a crisp message instead
        # of the last-resort "Unexpected error" banner. Deliberately does
        # not run the full validate_config — a missing `base_url` for
        # openai_compatible is a "fix me now" case that --key should still
        # let the user through so they can update the key alongside.
        merged = merge_defaults(raw)
        try:
            provider = _vision.get_provider(merged)
        except KeyError as exc:
            raise ConfigError(str(exc)) from None
        ui.info(f"  Provider: {provider.display_name}")
        current = raw.get("api_key") or raw.get(LEGACY_API_KEY_FIELD) or ""
        raw["api_key"] = _ask("  API key", current, secret=True)

    return _run_single_field_setter(path, "ScreenRecon set API key", setter)


def run_set_model(path: str | os.PathLike[str] | None = None) -> int:
    """Two-step: pick a provider (or keep the current one), then pick a model
    from that provider's shortlist (or type any custom ID). For the
    OpenAI-compatible provider, also collect a base URL (preset or custom)
    and a default model for the chosen endpoint.

    Leaves every other config field alone. The api_key is left intact — the
    user runs ``--key`` next to update it for the new provider.
    """
    from . import vision as _vision

    def setter(raw: dict[str, Any]) -> None:
        # Translate the dispatcher's KeyError on an unknown provider name
        # into a ConfigError so hand-edited configs get a crisp message.
        # Mirrors run_set_key — deliberately does not run full
        # validate_config, so an openai_compatible config with a missing
        # base_url can still be fixed via this exact flow.
        try:
            _vision.get_provider(merge_defaults(raw))
        except KeyError as exc:
            raise ConfigError(str(exc)) from None
        _prompt_provider_and_model(raw)

    return _run_single_field_setter(path, "ScreenRecon set model", setter)


def _prompt_provider_and_model(target: dict[str, Any]) -> None:
    """Ask 'which provider' then 'which model'; mutate ``target`` in place.

    Shared between ``--configure`` step 3 and ``--model``. For
    ``openai_compatible`` also asks for the endpoint preset (or a custom
    base URL) and pre-fills a suggested model. Never contacts the network —
    verification happens later (only in ``--configure``, and only if the
    caller runs ``vision.verify_key(cfg)`` after).
    """
    from . import vision as _vision

    provider_options: list[tuple[str, str, str]] = [
        (p.display_name, p.name, "") for p in _vision.known_providers()
    ]
    current_provider = _vision.get_provider(target).name
    chosen_provider = _ask_choice(
        "AI provider",
        provider_options,
        current_provider,
        default=current_provider,
    )
    target["provider"] = chosen_provider

    if chosen_provider == "openai_compatible":
        model_from_preset = _prompt_compat_endpoint(target)
        if model_from_preset:
            # The preset already pre-filled a matching vision model; asking
            # again would be "you already told me this" — skip the model
            # prompt entirely. If the user wants to override, they can run
            # --model again and pick a custom URL (which returns None here).
            return

    model_options = MODEL_CHOICES_BY_PROVIDER.get(chosen_provider, [])
    current_model = str(target.get("model") or DEFAULT_MODEL)
    if not model_options:
        # No curated list for this provider (openai_compatible after a custom
        # endpoint): fall back to a plain text prompt with the current value
        # as the default.
        target["model"] = _ask("  model ID", current_model)
    else:
        default_value = model_options[-1][1] if current_model not in {
            v for _, v, _ in model_options
        } else current_model
        target["model"] = _ask_choice(
            "model",
            model_options,
            current_model,
            default=default_value,
        )


def _prompt_compat_endpoint(target: dict[str, Any]) -> str | None:
    """Ask which OpenAI-compatible endpoint to talk to.

    Returns the preset's default model name when the user picked a preset
    (numbered or typed by label) — caller uses that as the "model already
    picked" signal. Returns ``None`` when the user typed a custom base URL
    or chose "keep current", so the caller re-prompts for the model.

    A numbered preset picks a verified base URL + a suggested vision model
    in one step; typing text is treated as a preset label if it matches, or
    a custom base URL otherwise.
    """
    presets = list(COMPAT_PRESETS.items())
    current_base = str(target.get("base_url") or "")
    current_label = ""
    for label, (url, _model, _note) in presets:
        if url == current_base:
            current_label = label
            break

    ui.info("  Endpoint presets:")
    for idx, (label, (url, _model, note)) in enumerate(presets, start=1):
        ui.info(f"    {idx}) {label:<10} {url}  ({note})")
    ui.info(f"    {len(presets) + 1}) (keep current — {current_base or 'unset'})")

    prompt_hint = current_label or str(len(presets) + 1)
    for _ in range(MAX_PROMPT_RETRIES):
        answer = _ask(f"    Enter 1-{len(presets) + 1} or type any base URL", prompt_hint)
        answer = answer.strip()
        if not answer:
            continue
        if answer.isdigit():
            number = int(answer)
            if 1 <= number <= len(presets):
                label, (url, default_model, _note) = presets[number - 1]
                target["base_url"] = url
                target["model"] = default_model
                return default_model
            if number == len(presets) + 1:
                # Keep current base URL; model prompt handled by the caller.
                return None
            ui.warn(f"    Choice must be 1-{len(presets) + 1}; try again.")
            continue
        # Try preset by label first, otherwise treat as a raw URL.
        if answer.lower() in COMPAT_PRESETS:
            url, default_model, _ = COMPAT_PRESETS[answer.lower()]
            target["base_url"] = url
            target["model"] = default_model
            return default_model
        target["base_url"] = answer
        return None
    raise WizardAborted("Endpoint URL: too many invalid answers, giving up.")


def run_set_prompt(path: str | os.PathLike[str] | None = None) -> int:
    """Prompt for a new default prompt string and save it; leave every other field alone."""
    def setter(raw: dict[str, Any]) -> None:
        current = str(raw.get("prompt", DEFAULT_PROMPT))
        raw["prompt"] = _ask("  default prompt", current)

    return _run_single_field_setter(path, "ScreenRecon set default prompt", setter)


def run_set_dwell(path: str | os.PathLike[str] | None = None) -> int:
    """Prompt for a new dwell-seconds value and save it; leave every other field alone."""
    def setter(raw: dict[str, Any]) -> None:
        current = raw.get("dwell_seconds", DEFAULTS["dwell_seconds"])
        raw["dwell_seconds"] = _ask_float("  dwell seconds", current, minimum=0)

    return _run_single_field_setter(path, "ScreenRecon set dwell seconds", setter)


def run_set_save_dir(path: str | os.PathLike[str] | None = None) -> int:
    """Prompt for a new save directory and save it; leave every other field alone."""
    def setter(raw: dict[str, Any]) -> None:
        current = raw.get("save_dir", DEFAULT_SAVE_DIR)
        raw["save_dir"] = _ask("  save directory", current)

    return _run_single_field_setter(path, "ScreenRecon set save directory", setter)


def run_show(path: str | os.PathLike[str] | None = None) -> int:
    """Print the current config, credentials masked, and exit 0.

    Read-only companion to the single-field setters: a user who wants to know
    "what is set to what right now" no longer has to open the JSON file (and
    handle unfamiliar escaping) or re-run `--configure` just to see the
    current values. Credentials pass through :func:`ui.mask` so scrollback
    stays safe to share.

    Refuses if no config exists yet, matching the setters — nothing useful
    to show, and the message points at `--configure`.
    """
    from . import display

    resolved = config_path(path)
    raw = read_raw(resolved)
    if not raw:
        ui.error(
            f"No config at {resolved}. Run 'screenrecon --configure' to create one."
        )
        return 1

    cfg = merge_defaults(raw)

    from . import vision as _vision

    # Translate the dispatcher's KeyError on an unknown provider name into a
    # crisp ConfigError instead of the "Unexpected error: KeyError" banner
    # the last-resort handler would produce. Deliberately does not run the
    # full validate_config — --show is read-only and should still be able to
    # print a config that is *technically* invalid (e.g. openai_compatible
    # without base_url) so the user can see what needs fixing.
    try:
        provider = _vision.get_provider(cfg)
    except KeyError as exc:
        ui.error(str(exc))
        return 1

    region = cfg["region"]
    stored_monitor = raw.get("monitor") if isinstance(raw.get("monitor"), dict) else None
    monitor_annotation = (
        display.format_monitor_info(stored_monitor)
        or display.describe_region_monitor(region)
    )

    ui.rule("ScreenRecon config")
    ui.info(f"Config file: {resolved}")
    ui.info("")
    ui.info(
        f"  Region:          left={region.get('left')} top={region.get('top')} "
        f"width={region.get('width')} height={region.get('height')}{monitor_annotation}"
    )
    ui.info(f"  Dwell:           {cfg['dwell_seconds']} s")
    ui.info(f"  Provider:        {provider.display_name}")
    ui.info(f"  Model:           {cfg['model']}")
    if provider.name == "openai_compatible":
        ui.info(f"  Base URL:        {cfg.get('base_url') or '(unset)'}")
    ui.info(f"  Default prompt:  {cfg['prompt']}")
    ui.info(f"  Save directory:  {cfg['save_dir']}")
    ui.info(f"  API key:         {ui.mask(str(cfg.get('api_key') or ''))}")
    ui.info(f"  Telegram bot:    {ui.mask(str(cfg['telegram_bot_token']))}")
    ui.info(f"  Telegram chat:   {ui.mask(str(cfg['telegram_chat_id']))}")
    return 0


def run_set_telegram(path: str | os.PathLike[str] | None = None) -> int:
    """Prompt for both Telegram credentials and save them; leave every other field alone.

    The token and chat ID are prompted together because they are a matched pair
    — a valid token with the wrong chat ID delivers to somewhere the user did
    not intend. Splitting them into two flags would let the user change one
    without the other and silently misdeliver captures.
    """
    def setter(raw: dict[str, Any]) -> None:
        raw["telegram_bot_token"] = _ask(
            "  Telegram bot token",
            raw.get("telegram_bot_token", DEFAULTS["telegram_bot_token"]),
            secret=True,
        )
        raw["telegram_chat_id"] = _ask(
            "  Telegram chat ID",
            raw.get("telegram_chat_id", DEFAULTS["telegram_chat_id"]),
            secret=True,
        )

    return _run_single_field_setter(path, "ScreenRecon set Telegram credentials", setter)


def _run_wizard(
    path: str | os.PathLike[str] | None = None,
    picker_factory: PickerFactory | None = None,
) -> int:
    from . import notify, vision  # lazy: --help skips SDK
    from . import picker as picker_module

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
    ui.info("Press Enter to keep the current value. Nothing is saved until every step is done.\n")

    ui.info("1) Watched region")
    stored_monitor = raw.get("monitor") if isinstance(raw.get("monitor"), dict) else None
    cfg["region"] = _prompt_region(dict(cfg["region"]), stored_monitor, picker_factory)
    _apply_monitor_info(cfg, cfg["region"])

    ui.info("\n2) Trigger")
    ui.info(
        "   A capture fires when the cursor stays in the region for this many"
        " seconds. After a capture, the cursor must leave and re-enter the"
        " region to arm the next one."
    )
    cfg["dwell_seconds"] = _ask_float("  dwell seconds", cfg["dwell_seconds"], minimum=0)

    ui.info("\n3) AI provider and model")
    _prompt_provider_and_model(cfg)

    ui.info("\n4) Default prompt")
    ui.info("   The system prompt sent with every capture. See examples/prompts.json in the repo for starter prompts you can copy.")
    cfg["prompt"] = _ask("  default prompt", str(cfg["prompt"]))

    ui.info("\n5) Credentials")
    ui.info(
        "   Characters are visible while typing so paste works. Clear your"
        " terminal history after setup if you plan to share screenshots or"
        " scrollback."
    )
    current_key = str(cfg.get("api_key") or cfg.get(LEGACY_API_KEY_FIELD) or "")
    cfg["api_key"] = _ask("  API key", current_key, secret=True)
    cfg["telegram_bot_token"] = _ask("  Telegram bot token", cfg["telegram_bot_token"], secret=True)
    cfg["telegram_chat_id"] = _ask("  Telegram chat ID", cfg["telegram_chat_id"], secret=True)

    ui.info("\n6) Local archive")
    cfg["save_dir"] = _ask("  save directory", cfg["save_dir"])

    ui.rule("Verifying credentials")
    ok_ai, msg_ai = vision.verify_key(cfg)
    ui.info(("  OK   " if ok_ai else "  FAIL ") + f"AI: {msg_ai}")
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
    if not (ok_ai and ok_tg):
        ui.warn("Some credentials failed verification. The config was saved anyway.")
    return 0
