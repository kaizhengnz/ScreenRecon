"""Config tests (design doc 8.1): missing fields, invalid values, env override, masking."""

from __future__ import annotations

import json

import pytest

from screenrecon import config, ui


def write_config(tmp_path, payload) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def valid_payload(**overrides):
    payload = {
        "region": {"left": 10, "top": 20, "width": 300, "height": 200},
        "anthropic_api_key": "sk-ant-test-key-value",
        "telegram_bot_token": "123456:ABCDEF-token-value",
        "telegram_chat_id": "987654321",
        "save_dir": "~/ScreenRecon",
        "prompt": "Describe this screenshot.",
        "prompts": {"log": "Find the error and explain it."},
        "dwell_seconds": 3,
        "model": "claude-opus-5",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Loading and merging
# --------------------------------------------------------------------------- #


def test_missing_file_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    cfg = config.load(tmp_path / "does-not-exist.json")
    assert cfg["region"] == config.DEFAULTS["region"]
    assert cfg["model"] == config.DEFAULT_MODEL
    assert cfg["anthropic_api_key"] == ""


def test_partial_region_is_merged_with_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = write_config(tmp_path, {"region": {"left": 5}})
    cfg = config.load(path)
    assert cfg["region"]["left"] == 5
    assert cfg["region"]["width"] == config.DEFAULTS["region"]["width"]


def test_invalid_json_reports_the_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.load(path)


def test_roundtrip_save_and_load(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = tmp_path / "nested" / "config.json"
    saved = config.save(valid_payload(), path)
    assert saved.exists()
    cfg = config.load(path)
    assert cfg["telegram_chat_id"] == "987654321"
    # Internal bookkeeping keys never reach the file.
    assert "_path" not in json.loads(saved.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        ({"region": {"left": 0, "top": 0, "width": 0, "height": 10}}, "region.width"),
        ({"region": {"left": 0, "top": 0, "width": 10, "height": -5}}, "region.height"),
        ({"region": {"left": "x", "top": 0, "width": 10, "height": 10}}, "region.left"),
        ({"dwell_seconds": 0}, "dwell_seconds"),
        ({"dwell_seconds": -1}, "dwell_seconds"),
        ({"dwell_seconds": "soon"}, "dwell_seconds"),
        ({"model": ""}, "model"),
        ({"prompt": "   "}, "prompt"),
        ({"save_dir": ""}, "save_dir"),
    ],
)
def test_invalid_values_name_the_field(tmp_path, monkeypatch, payload, expected_field):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = write_config(tmp_path, valid_payload(**payload))
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(path)
    assert expected_field in str(excinfo.value)


def test_partially_specified_region_is_completed_from_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = write_config(tmp_path, {"region": {"left": 1, "top": 2, "width": 3}})
    cfg = config.load(path)
    assert cfg["region"]["width"] == 3
    assert cfg["region"]["height"] == config.DEFAULTS["region"]["height"]


def test_missing_region_field_is_reported_when_validated_directly():
    """load() fills gaps from defaults; validate_config still guards direct callers."""
    with pytest.raises(config.ConfigError, match="region.height"):
        config.validate_config(
            dict(
                config.DEFAULTS,
                region={"left": 1, "top": 2, "width": 3},
            )
        )


def test_negative_origin_is_allowed(tmp_path, monkeypatch):
    """Secondary monitors sit at negative coordinates; only width/height must be positive."""
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = write_config(
        tmp_path, valid_payload(region={"left": -1920, "top": -50, "width": 800, "height": 600})
    )
    cfg = config.load(path)
    assert cfg["region"]["left"] == -1920


def test_float_dwell_seconds_is_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = write_config(tmp_path, valid_payload(dwell_seconds=1.5))
    assert config.load(path)["dwell_seconds"] == 1.5


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field", ["anthropic_api_key", "telegram_bot_token", "telegram_chat_id"]
)
def test_missing_credential_blocks_startup(field):
    cfg = config.merge_defaults(valid_payload(**{field: ""}))
    with pytest.raises(config.ConfigError, match="--configure"):
        config.require_credentials(cfg)


def test_whitespace_only_credential_counts_as_missing():
    cfg = config.merge_defaults(valid_payload(telegram_chat_id="   "))
    with pytest.raises(config.ConfigError):
        config.require_credentials(cfg)


def test_complete_credentials_pass():
    config.require_credentials(config.merge_defaults(valid_payload()))


# --------------------------------------------------------------------------- #
# Environment override (FR-15)
# --------------------------------------------------------------------------- #


def test_env_var_overrides_file_key(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-ant-from-environment")
    path = write_config(tmp_path, valid_payload(anthropic_api_key="sk-ant-from-file"))
    assert config.load(path)["anthropic_api_key"] == "sk-ant-from-environment"


def test_empty_env_var_does_not_override(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "   ")
    path = write_config(tmp_path, valid_payload(anthropic_api_key="sk-ant-from-file"))
    assert config.load(path)["anthropic_api_key"] == "sk-ant-from-file"


def test_env_var_satisfies_credential_requirement(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_API_KEY, "sk-ant-from-environment")
    path = write_config(tmp_path, valid_payload(anthropic_api_key=""))
    config.require_credentials(config.load(path))


# --------------------------------------------------------------------------- #
# Prompt presets (FR-13)
# --------------------------------------------------------------------------- #


def test_mode_selects_preset():
    cfg = config.merge_defaults(valid_payload())
    assert config.resolve_prompt(cfg, "log") == "Find the error and explain it."


def test_no_mode_uses_default_prompt():
    cfg = config.merge_defaults(valid_payload())
    assert config.resolve_prompt(cfg, None) == "Describe this screenshot."
    assert config.resolve_prompt(cfg, "default") == "Describe this screenshot."


def test_unknown_mode_lists_available_presets():
    cfg = config.merge_defaults(valid_payload())
    with pytest.raises(config.ConfigError) as excinfo:
        config.resolve_prompt(cfg, "nope")
    assert "log" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Masking (NFR-3)
# --------------------------------------------------------------------------- #


def test_mask_shows_only_the_first_eight_characters():
    masked = ui.mask("sk-ant-api03-SECRET-TAIL")
    assert masked.startswith("sk-ant-a")
    assert "SECRET" not in masked
    assert "TAIL" not in masked


def test_mask_handles_empty_and_short_values():
    assert ui.mask("") == "(not set)"
    assert ui.mask(None) == "(not set)"
    assert "abc" not in ui.mask("abc")


def test_scrub_removes_credentials_from_arbitrary_text():
    token = "123456:AAHfake-telegram-token"
    text = f"HTTPSConnectionPool: url https://api.telegram.org/bot{token}/sendPhoto failed"
    scrubbed = ui.scrub(text, [token])
    assert token not in scrubbed
    assert "sendPhoto" in scrubbed


def test_scrub_ignores_short_or_empty_secrets():
    assert ui.scrub("nothing to do", ["", None, "abc"]) == "nothing to do"


def test_config_file_permissions_are_owner_only(tmp_path):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX permission bits do not apply on Windows")
    path = config.save(valid_payload(), tmp_path / "config.json")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
