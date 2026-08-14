"""Setup wizard behaviour (FR-11, NFR-3)."""

from __future__ import annotations

import json

import pytest

from screenrecon import config, notify, picker, vision


def _picker_factory(region):
    """Convenience: build a picker_factory callable returning a scripted picker."""

    def factory():
        return picker.ScriptedPicker(region)

    return factory


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted answers to `input()`; record which was used."""
    scripted: list[str] = []
    used: list[tuple[str, str]] = []

    def take(kind, prompt):
        if not scripted:
            raise EOFError
        value = scripted.pop(0)
        used.append((kind, prompt))
        return value

    monkeypatch.setattr("builtins.input", lambda prompt="": take("input", prompt))
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


def test_secrets_go_through_input_not_getpass(answers):
    """Secrets now use `input` on all platforms — hiding characters broke paste
    on Windows and never added real safety for keys made of printable ASCII.
    The [hint] and the "received:" echo are still masked (`ui.mask`).
    """
    scripted, used = answers
    scripted.append("sk-ant-secret-value")
    config._ask("api key", "", secret=True)
    assert used[0][0] == "input"


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


def test_choice_picks_a_preset_by_number(answers):
    """Numbered input `2` returns the second preset's value."""
    scripted, _ = answers
    scripted.append("2")
    assert (
        config._ask_choice("AI model", config.MODEL_CHOICES, "claude-opus-5")
        == "claude-haiku-4-5"
    )


def test_choice_enter_keeps_current_via_last_index(answers):
    """Enter defaults to index N+1 (current), so the current value survives."""
    scripted, _ = answers
    scripted.append("")
    assert (
        config._ask_choice("AI model", config.MODEL_CHOICES, "claude-sonnet-5-preview")
        == "claude-sonnet-5-preview"
    )


def test_choice_typed_text_becomes_a_custom_value(answers):
    """Non-numeric input passes through so future models work without a code change."""
    scripted, _ = answers
    scripted.append("claude-brand-new-model")
    assert (
        config._ask_choice("AI model", config.MODEL_CHOICES, "claude-opus-5")
        == "claude-brand-new-model"
    )


def test_choice_out_of_range_number_retries(answers):
    """A number outside 1..N+1 warns and re-prompts rather than accepting it."""
    scripted, _ = answers
    scripted.extend(["9", "0", "2"])
    assert (
        config._ask_choice("AI model", config.MODEL_CHOICES, "claude-opus-5")
        == "claude-haiku-4-5"
    )


def test_choice_gives_up_after_repeated_invalid_answers(answers):
    scripted, _ = answers
    scripted.extend(["9"] * (config.MAX_PROMPT_RETRIES + 2))
    with pytest.raises(config.WizardAborted):
        config._ask_choice("AI model", config.MODEL_CHOICES, "claude-opus-5")


def test_prompt_choice_by_number_returns_the_prompt_text(answers):
    """Prompt presets store a short label and a full prompt string; picking 1
    returns the full text, not the label.
    """
    scripted, _ = answers
    scripted.append("1")
    result = config._ask_choice(
        "default prompt", config.PROMPT_CHOICES, "old current prompt"
    )
    assert result == config.PROMPT_CHOICES[0][1]
    assert result != config.PROMPT_CHOICES[0][0]  # not the label


def test_prompt_choice_enter_keeps_current(answers):
    scripted, _ = answers
    scripted.append("")
    assert (
        config._ask_choice("default prompt", config.PROMPT_CHOICES, "my custom prompt")
        == "my custom prompt"
    )


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
            "Y",  # update the region? Yes → picker runs
            "2.5",  # dwell seconds
            "claude-haiku-4-5",  # model
            "read the screen",  # prompt
            "sk-ant-wizard-key",  # api key
            "123456:wizard-token",  # bot token
            "987654321",  # chat id
            str(tmp_path / "shots"),  # save dir
        ]
    )
    picked = {"left": 10, "top": 20, "width": 300, "height": 200}

    assert config.run_wizard(path, picker_factory=_picker_factory(picked)) == 0

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["region"] == picked
    assert saved["dwell_seconds"] == 2.5
    assert saved["model"] == "claude-haiku-4-5"
    assert saved["anthropic_api_key"] == "sk-ant-wizard-key"
    assert saved["telegram_chat_id"] == "987654321"

    # NFR-3: nothing the user typed as a credential is echoed back.
    output = capsys.readouterr().out
    assert "sk-ant-wizard-key" not in output
    assert "123456:wizard-token" not in output


def test_wizard_shows_centered_default_when_no_region_is_saved(
    tmp_path, answers, offline, monkeypatch, capsys
):
    """First-time user (no config file) sees a 640x480 region centred on the
    cursor's monitor as "Current", not the hardcoded 100/100/600/400.
    """
    scripted, _ = answers
    path = tmp_path / "config.json"  # no file yet
    monkeypatch.setattr("screenrecon.platform.ensure_reader", lambda: None)
    monkeypatch.setattr("screenrecon.platform.get_cursor_pos", lambda: (500, 500))
    monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: monitors)
    monkeypatch.setattr(
        "screenrecon.display.find_monitor_containing", lambda x, y, mons=None: monitors[0]
    )
    scripted.extend(
        [
            "n",  # decline the picker; keep the centred default that was shown
            "3",
            "claude-opus-5",
            "p",
            "k",
            "t",
            "c",
            str(tmp_path),
        ]
    )

    def exploding_factory():
        raise AssertionError("picker was invoked despite user answering 'n'")

    assert config.run_wizard(path, picker_factory=exploding_factory) == 0

    expected = {
        "left": (1920 - picker.DEFAULT_WIDTH) // 2,
        "top": (1080 - picker.DEFAULT_HEIGHT) // 2,
        "width": picker.DEFAULT_WIDTH,
        "height": picker.DEFAULT_HEIGHT,
    }
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["region"] == expected
    out = capsys.readouterr().out
    assert f"left={expected['left']}" in out
    assert f"top={expected['top']}" in out
    assert "(on monitor 1 of 1)" in out  # Current line names the monitor


def test_wizard_keeps_current_region_when_user_declines_the_picker(
    tmp_path, answers, offline
):
    """Answering 'n' at the "Update this region?" prompt must not open the picker."""
    scripted, _ = answers
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "region": {"left": 50, "top": 60, "width": 400, "height": 300},
                "anthropic_api_key": "old-key",
                "telegram_bot_token": "old-token",
                "telegram_chat_id": "old-chat",
                "save_dir": str(tmp_path),
                "prompt": "p",
                "prompts": {},
                "dwell_seconds": 3,
                "model": "claude-opus-5",
            }
        ),
        encoding="utf-8",
    )
    scripted.extend(
        [
            "n",  # do NOT update the region
            "3",  # dwell
            "claude-opus-5",
            "p",
            "k",
            "t",
            "c",
            str(tmp_path),
        ]
    )

    # The picker factory should never even be called; hand a sentinel that would fail if it were.
    def exploding_factory():
        raise AssertionError("picker was invoked despite user answering 'n'")

    assert config.run_wizard(path, picker_factory=exploding_factory) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["region"] == {"left": 50, "top": 60, "width": 400, "height": 300}


def test_wizard_falls_back_to_default_region_on_picker_cancel(
    tmp_path, answers, offline, monkeypatch
):
    """Picker returned None -> wizard uses default_region_at (centered on cursor's monitor)."""
    scripted, _ = answers
    path = tmp_path / "config.json"
    scripted.extend(
        ["Y", "3", "claude-opus-5", "p", "k", "t", "c", str(tmp_path)]
    )

    # Cursor at (500, 500) on the only known monitor.
    monkeypatch.setattr("screenrecon.platform.ensure_reader", lambda: None)
    monkeypatch.setattr("screenrecon.platform.get_cursor_pos", lambda: (500, 500))
    monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
    monkeypatch.setattr("screenrecon.display.enumerate_monitors", lambda: monitors)
    monkeypatch.setattr(
        "screenrecon.display.find_monitor_containing", lambda x, y, mons=None: monitors[0]
    )

    assert config.run_wizard(path, picker_factory=_picker_factory(None)) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["region"] == {
        "left": (1920 - picker.DEFAULT_WIDTH) // 2,
        "top": (1080 - picker.DEFAULT_HEIGHT) // 2,
        "width": picker.DEFAULT_WIDTH,
        "height": picker.DEFAULT_HEIGHT,
    }


def test_wizard_keeps_current_region_when_picker_cannot_open(
    tmp_path, answers, offline, capsys
):
    """PickerError (missing tkinter / no display) must not crash the wizard —
    the user should see the error and end up with the current region intact.
    """
    scripted, _ = answers
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "region": {"left": 50, "top": 60, "width": 400, "height": 300},
                "anthropic_api_key": "old-key",
                "telegram_bot_token": "old-token",
                "telegram_chat_id": "old-chat",
                "save_dir": str(tmp_path),
                "prompt": "p",
                "prompts": {},
                "dwell_seconds": 3,
                "model": "claude-opus-5",
            }
        ),
        encoding="utf-8",
    )
    scripted.extend(
        [
            "Y",  # open the picker
            "3",
            "claude-opus-5",
            "p",
            "k",
            "t",
            "c",
            str(tmp_path),
        ]
    )

    class ExplodingPicker:
        def pick(self):
            raise picker.PickerError("tkinter is not available on this Python")

    assert config.run_wizard(path, picker_factory=lambda: ExplodingPicker()) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["region"] == {"left": 50, "top": 60, "width": 400, "height": 300}
    output = capsys.readouterr().out
    assert "tkinter is not available" in output
    assert "Keeping the current region" in output


def test_wizard_aborts_cleanly_on_closed_stdin(tmp_path, answers, offline, capsys):
    path = tmp_path / "config.json"
    assert config.run_wizard(path, picker_factory=_picker_factory(None)) == 1
    assert not path.exists()
    assert "stdin is closed" in capsys.readouterr().err


def test_wizard_reports_failed_verification_but_still_saves(tmp_path, answers, monkeypatch, capsys):
    scripted, _ = answers
    monkeypatch.setattr(vision, "verify_key", lambda key, model: (False, "key is invalid"))
    monkeypatch.setattr(notify, "verify_credentials", lambda token, chat: (True, "ok"))
    path = tmp_path / "config.json"
    scripted.extend(
        ["Y", "3", "claude-opus-5", "p", "k", "t", "c", str(tmp_path)]
    )
    picked = {"left": 10, "top": 20, "width": 300, "height": 200}

    assert config.run_wizard(path, picker_factory=_picker_factory(picked)) == 0
    assert path.exists()
    output = capsys.readouterr().out
    assert "key is invalid" in output
    assert "[warn]" in output


# --------------------------------------------------------------------------- #
# run_set_region — --screen flag
# --------------------------------------------------------------------------- #


def test_set_region_updates_only_the_region_field(tmp_path):
    """--screen must not touch credentials, prompts, or any other field."""
    path = tmp_path / "config.json"
    original = {
        "region": {"left": 50, "top": 60, "width": 400, "height": 300},
        "anthropic_api_key": "old-key",
        "telegram_bot_token": "old-token",
        "telegram_chat_id": "old-chat",
        "save_dir": str(tmp_path),
        "prompt": "keep me",
        "prompts": {"log": "find errors"},
        "dwell_seconds": 2.5,
        "model": "claude-opus-5",
    }
    path.write_text(json.dumps(original), encoding="utf-8")

    picked = {"left": 10, "top": 20, "width": 800, "height": 600}
    assert (
        config.run_set_region(path, picker_factory=_picker_factory(picked)) == 0
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["region"] == picked
    # Every other field is untouched, byte-for-byte.
    for key, value in original.items():
        if key == "region":
            continue
        assert saved[key] == value


def test_set_region_refuses_when_no_config_exists(tmp_path, capsys):
    """--screen is not a first-run flow — the user needs credentials first."""
    path = tmp_path / "config.json"  # does not exist
    assert config.run_set_region(path, picker_factory=_picker_factory(None)) == 1
    err = capsys.readouterr().err
    assert "--configure" in err
    assert not path.exists()


def test_set_region_keeps_partial_config_partial(tmp_path):
    """A partial config (some fields missing) must stay partial after --screen —
    running --screen must not silently pad the file with defaults for fields
    the user has deliberately not set yet. The ``monitor`` field is a
    picker-owned companion of the region and is allowed to appear."""
    path = tmp_path / "config.json"
    partial = {
        "region": {"left": 0, "top": 0, "width": 100, "height": 100},
        "anthropic_api_key": "just-a-key",
    }
    path.write_text(json.dumps(partial), encoding="utf-8")

    picked = {"left": 10, "top": 20, "width": 300, "height": 200}
    assert (
        config.run_set_region(path, picker_factory=_picker_factory(picked)) == 0
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved.keys()) <= {"region", "anthropic_api_key", "monitor"}
    assert saved["region"] == picked
    assert saved["anthropic_api_key"] == "just-a-key"


def test_set_region_records_the_monitor(tmp_path, monkeypatch):
    """--screen must record which monitor the picked region belongs to, so the
    watcher can display it without recomputing (and without drifting if the
    monitor topology changes later)."""
    from screenrecon import display

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "region": {"left": 0, "top": 0, "width": 100, "height": 100},
                "anthropic_api_key": "k",
            }
        ),
        encoding="utf-8",
    )
    monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": 1920, "top": 0, "width": 2560, "height": 1440},
    ]
    monkeypatch.setattr(display, "enumerate_monitors", lambda: monitors)

    # Picked centre (3000, 700) lies on monitor 2.
    picked = {"left": 2500, "top": 500, "width": 1000, "height": 400}
    assert (
        config.run_set_region(path, picker_factory=_picker_factory(picked)) == 0
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["monitor"] == {"index": 2, "of": 2}


def test_set_region_clears_a_stale_monitor_when_enumeration_fails(tmp_path, monkeypatch):
    """A previously-stored monitor annotation must not linger when we cannot
    recompute — leaving stale info would defeat the whole point of storing it."""
    from screenrecon import display

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "region": {"left": 0, "top": 0, "width": 100, "height": 100},
                "monitor": {"index": 1, "of": 2},
                "anthropic_api_key": "k",
            }
        ),
        encoding="utf-8",
    )

    def raise_():
        raise RuntimeError("mss cannot enumerate here")

    monkeypatch.setattr(display, "enumerate_monitors", raise_)

    picked = {"left": 10, "top": 20, "width": 300, "height": 200}
    assert (
        config.run_set_region(path, picker_factory=_picker_factory(picked)) == 0
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "monitor" not in saved
