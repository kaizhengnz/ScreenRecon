"""Argument routing, exit codes and the last-resort error handler (design doc 5.8)."""

from __future__ import annotations

import json

import pytest

from screenrecon import cli, config, watcher

CONFIG = {
    "region": {"left": 10, "top": 20, "width": 300, "height": 200},
    "anthropic_api_key": "sk-ant-test-key-value",
    "telegram_bot_token": "123456:test-bot-token",
    "telegram_chat_id": "987654321",
    "save_dir": "~/ScreenRecon",
    "prompt": "default prompt",
    "prompts": {"log": "find errors"},
    "dwell_seconds": 3,
    "model": "claude-opus-5",
}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return str(path)


@pytest.fixture
def calls(monkeypatch):
    """Capture what main() dispatches to, without running any of it."""
    recorded: dict[str, object] = {}

    def record(name, value):
        recorded[name] = value
        return 0

    monkeypatch.setattr(watcher, "run", lambda cfg, prompt: record("watch", prompt))
    monkeypatch.setattr(watcher, "run_ask", lambda cfg, question: record("ask", question))
    monkeypatch.setattr(config, "run_wizard", lambda path: record("wizard", path))
    return recorded


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_no_arguments_starts_the_watch_loop(config_file, calls):
    assert cli.main(["--config", config_file]) == 0
    assert calls["watch"] == "default prompt"


def test_mode_selects_the_preset(config_file, calls):
    assert cli.main(["--config", config_file, "--mode", "log"]) == 0
    assert calls["watch"] == "find errors"


def test_configure_runs_the_wizard(config_file, calls):
    assert cli.main(["--config", config_file, "--configure"]) == 0
    assert calls["wizard"] == config_file


def test_ask_passes_the_joined_question(config_file, calls):
    assert cli.main(["--config", config_file, "ask", "what", "is", "this"]) == 0
    assert calls["ask"] == "what is this"


def test_ask_without_a_question_is_interactive(config_file, calls):
    assert cli.main(["--config", config_file, "ask"]) == 0
    assert calls["ask"] is None


def test_ask_with_a_mode_uses_the_preset_as_the_question(config_file, calls):
    assert cli.main(["--config", config_file, "--mode", "log", "ask"]) == 0
    assert calls["ask"] == "find errors"


def test_show_cursor_flag_gets_a_friendly_error(capsys):
    """--show-cursor was removed; the setup wizard now has a picker. Guide the user."""
    assert cli.main(["--show-cursor"]) == 2
    err = capsys.readouterr().err
    assert "--show-cursor was removed" in err
    assert "--configure" in err


# --------------------------------------------------------------------------- #
# Rejected combinations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ["--configure", "ask", "q"],
        ["--mode", "log", "--configure"],
    ],
)
def test_conflicting_flags_exit_two(argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2


def test_unknown_mode_is_an_error_when_watching(config_file, calls):
    assert cli.main(["--config", config_file, "--mode", "nope"]) == 1
    assert "watch" not in calls


def test_unknown_mode_is_an_error_for_ask_too(config_file, calls, capsys):
    """A typo must not silently fall back to the default prompt."""
    assert cli.main(["--config", config_file, "--mode", "nope", "ask", "q"]) == 1
    assert "ask" not in calls
    assert "nope" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Exit codes and error handling
# --------------------------------------------------------------------------- #


def test_missing_credentials_exit_one(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**CONFIG, "anthropic_api_key": ""}), encoding="utf-8")
    assert cli.main(["--config", str(path)]) == 1
    assert "--configure" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130(config_file, monkeypatch):
    monkeypatch.setattr(
        watcher, "run", lambda cfg, prompt: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    assert cli.main(["--config", config_file]) == 130


def test_unexpected_errors_are_caught_and_scrubbed(config_file, monkeypatch, capsys):
    """A traceback here would carry the credentials in its frame locals."""

    def explode(cfg, prompt):
        raise RuntimeError(f"boom with key {cfg['anthropic_api_key']}")

    monkeypatch.setattr(watcher, "run", explode)
    assert cli.main(["--config", config_file]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert CONFIG["anthropic_api_key"] not in combined
    assert "RuntimeError" in combined


def test_version_and_help_do_not_need_a_config():
    for argv in (["--version"], ["--help"]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == 0


def test_help_does_not_disclose_the_home_directory(capsys):
    """--help output is routinely pasted into bug reports."""
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "~/.config/screenrecon/config.json" in capsys.readouterr().out


def test_redirected_endpoint_is_announced(config_file, calls, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com")
    assert cli.main(["--config", config_file]) == 0
    assert "proxy.example.com" in capsys.readouterr().out
