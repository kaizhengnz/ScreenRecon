"""Argument routing, exit codes and the last-resort error handler (design doc 5.8)."""

from __future__ import annotations

import json

import pytest

from screenrecon import cli, config, watcher

CONFIG = {
    "region": {"left": 10, "top": 20, "width": 300, "height": 200},
    "api_key": "sk-ant-test-key-value",
    "telegram_bot_token": "123456:test-bot-token",
    "telegram_chat_id": "987654321",
    "save_dir": "~/ScreenRecon",
    "prompt": "default prompt",
    "dwell_seconds": 3,
    "model": "claude-opus-5",
}


@pytest.fixture
def config_file(tmp_path):
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

    def record_watch(cfg, prompt, *, debug=False):
        record("watch", prompt)
        record("debug", debug)
        return 0

    monkeypatch.setattr(watcher, "run", record_watch)
    monkeypatch.setattr(watcher, "run_ask", lambda cfg, question: record("ask", question))
    monkeypatch.setattr(config, "run_wizard", lambda path: record("wizard", path))
    monkeypatch.setattr(config, "run_set_region", lambda path: record("set_region", path))
    monkeypatch.setattr(config, "run_set_key", lambda path: record("set_key", path))
    monkeypatch.setattr(config, "run_set_model", lambda path: record("set_model", path))
    monkeypatch.setattr(config, "run_set_prompt", lambda path: record("set_prompt", path))
    monkeypatch.setattr(config, "run_set_dwell", lambda path: record("set_dwell", path))
    monkeypatch.setattr(config, "run_set_save_dir", lambda path: record("set_save_dir", path))
    monkeypatch.setattr(config, "run_set_telegram", lambda path: record("set_telegram", path))
    monkeypatch.setattr(config, "run_show", lambda path: record("show", path))
    return recorded


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def test_no_arguments_starts_the_watch_loop(config_file, calls):
    assert cli.main(["--config", config_file]) == 0
    assert calls["watch"] == "default prompt"
    assert calls["debug"] is False


def test_debug_flag_is_forwarded_to_the_watch_loop(config_file, calls):
    assert cli.main(["--config", config_file, "--debug"]) == 0
    assert calls["debug"] is True


def test_inline_prompt_replaces_the_default_for_this_run(config_file, calls):
    assert cli.main(["--config", config_file, "--prompt", "translate please"]) == 0
    assert calls["watch"] == "translate please"


def test_inline_prompt_with_ask_becomes_the_question(config_file, calls):
    assert cli.main(["--config", config_file, "--prompt", "look for errors", "ask"]) == 0
    assert calls["ask"] == "look for errors"


def test_inline_prompt_yields_to_positional_ask_question(config_file, calls):
    assert cli.main(
        ["--config", config_file, "--prompt", "look for errors", "ask", "no", "actually", "translate"]
    ) == 0
    assert calls["ask"] == "no actually translate"


def test_bare_prompt_runs_the_setter(config_file, calls):
    assert cli.main(["--config", config_file, "--prompt"]) == 0
    assert calls["set_prompt"] == config_file
    assert "watch" not in calls


def test_configure_runs_the_wizard(config_file, calls):
    assert cli.main(["--config", config_file, "--configure"]) == 0
    assert calls["wizard"] == config_file


def test_screen_re_picks_only_the_region(config_file, calls):
    assert cli.main(["--config", config_file, "--screen"]) == 0
    assert calls["set_region"] == config_file
    assert "watch" not in calls


def test_key_sets_only_the_api_key(config_file, calls):
    assert cli.main(["--config", config_file, "--key"]) == 0
    assert calls["set_key"] == config_file
    assert "watch" not in calls


def test_model_sets_only_the_model(config_file, calls):
    assert cli.main(["--config", config_file, "--model"]) == 0
    assert calls["set_model"] == config_file
    assert "watch" not in calls


@pytest.mark.parametrize(
    ("flag", "recorded_key"),
    [
        ("--dwell", "set_dwell"),
        ("--save-dir", "set_save_dir"),
        ("--telegram", "set_telegram"),
    ],
)
def test_single_field_setters_route_to_their_config_helpers(
    config_file, calls, flag, recorded_key
):
    assert cli.main(["--config", config_file, flag]) == 0
    assert calls[recorded_key] == config_file
    assert "watch" not in calls


def test_show_prints_config_and_skips_the_watch_loop(config_file, calls):
    assert cli.main(["--config", config_file, "--show"]) == 0
    assert calls["show"] == config_file
    assert "watch" not in calls


def test_ask_passes_the_joined_question(config_file, calls):
    assert cli.main(["--config", config_file, "ask", "what", "is", "this"]) == 0
    assert calls["ask"] == "what is this"


def test_ask_without_a_question_is_interactive(config_file, calls):
    assert cli.main(["--config", config_file, "ask"]) == 0
    assert calls["ask"] is None


# --------------------------------------------------------------------------- #
# Rejected combinations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        ["--configure", "ask", "q"],
        ["--screen", "--configure"],
        ["--screen", "--debug"],
        ["--screen", "ask", "q"],
        ["--key", "--configure"],
        ["--model", "--debug"],
        ["--screen", "--key"],
        ["--screen", "--model"],
        ["--key", "--model"],
        ["--prompt", "--dwell"],
        ["--save-dir", "--telegram"],
        ["--telegram", "ask", "q"],
        ["--prompt", "--configure"],
        ["--show", "--configure"],
        ["--show", "--screen"],
        ["--show", "ask", "q"],
        ["--prompt", "text", "--dwell"],
        ["--show", "--prompt", "text"],
    ],
)
def test_conflicting_flags_exit_two(argv):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# Exit codes and error handling
# --------------------------------------------------------------------------- #


def test_missing_credentials_exit_one(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**CONFIG, "api_key": ""}), encoding="utf-8")
    assert cli.main(["--config", str(path)]) == 1
    assert "--configure" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130(config_file, monkeypatch):
    def interrupt(cfg, prompt, *, debug=False):
        raise KeyboardInterrupt()

    monkeypatch.setattr(watcher, "run", interrupt)
    assert cli.main(["--config", config_file]) == 130


def test_unexpected_errors_are_caught_and_scrubbed(config_file, monkeypatch, capsys):
    """A traceback here would carry the credentials in its frame locals."""

    def explode(cfg, prompt, *, debug=False):
        raise RuntimeError(f"boom with key {cfg['api_key']}")

    monkeypatch.setattr(watcher, "run", explode)
    assert cli.main(["--config", config_file]) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert CONFIG["api_key"] not in combined
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
