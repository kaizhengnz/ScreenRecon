"""Setup wizard behaviour (FR-11, NFR-3)."""

from __future__ import annotations

import json

import pytest

from screenrecon import config, notify, vision


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted answers to input() and getpass(); record which was used."""
    scripted: list[str] = []
    used: list[tuple[str, str]] = []

    def take(kind, prompt):
        if not scripted:
            raise EOFError
        value = scripted.pop(0)
        used.append((kind, prompt))
        return value

    monkeypatch.setattr("builtins.input", lambda prompt="": take("input", prompt))
    monkeypatch.setattr(config.getpass, "getpass", lambda prompt="": take("getpass", prompt))
    return scripted, used


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(vision, "verify_key", lambda key, model: (True, "ok"))
    monkeypatch.setattr(notify, "verify_credentials", lambda token, chat: (True, "ok"))


# --------------------------------------------------------------------------- #
# _ask primitives
# --------------------------------------------------------------------------- #


def test_enter_keeps_a_current_value_of_zero(answers):
    """A region at the top-left corner is the common case; Enter must keep 0."""
    scripted, _ = answers
    scripted.append("")
    assert config._ask("region left", 0) == "0"


def test_enter_keeps_a_normal_current_value(answers):
    scripted, _ = answers
    scripted.append("")
    assert config._ask("region left", 100) == "100"


def test_an_answer_replaces_the_current_value(answers):
    scripted, _ = answers
    scripted.append("250")
    assert config._ask("region left", 100) == "250"


def test_secrets_are_read_without_echo(answers):
    """NFR-3: a typed credential must not reach the terminal or shell history."""
    scripted, used = answers
    scripted.append("sk-ant-secret-value")
    config._ask("api key", "", secret=True)
    assert used[0][0] == "getpass"


def test_the_current_secret_is_only_ever_shown_masked(answers):
    scripted, used = answers
    scripted.append("")
    config._ask("api key", "sk-ant-api03-SECRET-TAIL", secret=True)
    prompt = used[0][1]
    assert "SECRET" not in prompt
    assert "sk-ant-a" in prompt


def test_closed_stdin_aborts_instead_of_looping(answers):
    """`screenrecon --configure < /dev/null` must not spin forever."""
    with pytest.raises(config.WizardAborted):
        config._ask_int("region width", "not-a-number")


def test_repeated_invalid_answers_give_up(answers):
    scripted, _ = answers
    scripted.extend(["x"] * (config.MAX_PROMPT_RETRIES + 2))
    with pytest.raises(config.WizardAborted):
        config._ask_int("region width", 600)


def test_minimum_is_enforced(answers):
    scripted, _ = answers
    scripted.extend(["0", "-5", "800"])
    assert config._ask_int("region width", 600, minimum=1) == 800


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #


def test_wizard_writes_every_answer(tmp_path, answers, offline, capsys):
    scripted, _ = answers
    path = tmp_path / "config.json"
    scripted.extend(
        [
            "10",  # left
            "20",  # top
            "300",  # width
            "200",  # height
            "2.5",  # dwell seconds
            "claude-haiku-4-5",  # model
            "read the screen",  # prompt
            "sk-ant-wizard-key",  # api key
            "123456:wizard-token",  # bot token
            "987654321",  # chat id
            str(tmp_path / "shots"),  # save dir
        ]
    )

    assert config.run_wizard(path) == 0

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["region"] == {"left": 10, "top": 20, "width": 300, "height": 200}
    assert saved["dwell_seconds"] == 2.5
    assert saved["model"] == "claude-haiku-4-5"
    assert saved["anthropic_api_key"] == "sk-ant-wizard-key"
    assert saved["telegram_chat_id"] == "987654321"

    # NFR-3: nothing the user typed as a credential is echoed back.
    output = capsys.readouterr().out
    assert "sk-ant-wizard-key" not in output
    assert "123456:wizard-token" not in output


def test_wizard_aborts_cleanly_on_closed_stdin(tmp_path, answers, offline, capsys):
    path = tmp_path / "config.json"
    assert config.run_wizard(path) == 1
    assert not path.exists()
    assert "stdin is closed" in capsys.readouterr().err


def test_wizard_reports_failed_verification_but_still_saves(tmp_path, answers, monkeypatch, capsys):
    scripted, _ = answers
    monkeypatch.setattr(vision, "verify_key", lambda key, model: (False, "key is invalid"))
    monkeypatch.setattr(notify, "verify_credentials", lambda token, chat: (True, "ok"))
    path = tmp_path / "config.json"
    scripted.extend(
        ["10", "20", "300", "200", "3", "claude-opus-5", "p", "k", "t", "c", str(tmp_path)]
    )

    assert config.run_wizard(path) == 0
    assert path.exists()
    output = capsys.readouterr().out
    assert "key is invalid" in output
    assert "[warn]" in output
