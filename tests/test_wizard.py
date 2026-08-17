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
    monkeypatch.setattr(vision, "verify_key", lambda cfg: (True, "ok"))
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
        config._ask_choice(
            "AI model",
            config.MODEL_CHOICES_BY_PROVIDER["anthropic"],
            "claude-opus-5",
        )
        == "claude-haiku-4-5"
    )


def test_choice_enter_keeps_current_via_last_index(answers):
    """Enter defaults to index N+1 (current), so the current value survives."""
    scripted, _ = answers
    scripted.append("")
    assert (
        config._ask_choice(
            "AI model",
            config.MODEL_CHOICES_BY_PROVIDER["anthropic"],
            "claude-sonnet-5-preview",
        )
        == "claude-sonnet-5-preview"
    )


def test_choice_typed_text_becomes_a_custom_value(answers):
    """Non-numeric input passes through so future models work without a code change."""
    scripted, _ = answers
    scripted.append("claude-brand-new-model")
    assert (
        config._ask_choice(
            "AI model",
            config.MODEL_CHOICES_BY_PROVIDER["anthropic"],
            "claude-opus-5",
        )
        == "claude-brand-new-model"
    )


def test_choice_out_of_range_number_retries(answers):
    """A number outside 1..N+1 warns and re-prompts rather than accepting it."""
    scripted, _ = answers
    scripted.extend(["9", "0", "2"])
    assert (
        config._ask_choice(
            "AI model",
            config.MODEL_CHOICES_BY_PROVIDER["anthropic"],
            "claude-opus-5",
        )
        == "claude-haiku-4-5"
    )


def test_choice_gives_up_after_repeated_invalid_answers(answers):
    scripted, _ = answers
    scripted.extend(["9"] * (config.MAX_PROMPT_RETRIES + 2))
    with pytest.raises(config.WizardAborted):
        config._ask_choice(
            "AI model",
            config.MODEL_CHOICES_BY_PROVIDER["anthropic"],
            "claude-opus-5",
        )


def test_prompt_choice_by_number_returns_the_prompt_text(answers):
    """Picking a numbered entry returns its full prompt string, not the label."""
    scripted, _ = answers
    scripted.append("1")
    result = config._ask_prompt("old current prompt")
    assert result == config.PROMPT_CHOICES[0][1]
    assert result != config.PROMPT_CHOICES[0][0]


def test_prompt_choice_enter_keeps_current(answers):
    """Enter maps to "keep current" regardless of what the current value is."""
    scripted, _ = answers
    scripted.append("")
    assert config._ask_prompt("my custom prompt") == "my custom prompt"


def test_prompt_choice_typed_text_becomes_a_custom_value(answers):
    """Non-numeric input passes through as the new prompt verbatim."""
    scripted, _ = answers
    scripted.append("Find bugs in the screenshot.")
    assert config._ask_prompt("old current") == "Find bugs in the screenshot."


def test_prompt_choices_have_the_expected_shape():
    """Every entry is a (label, value, note) triple; label and value are non-empty strings."""
    assert len(config.PROMPT_CHOICES) >= 4
    for entry in config.PROMPT_CHOICES:
        label, value, _ = entry
        assert isinstance(label, str) and label
        assert isinstance(value, str) and value
    labels = [label for label, _, _ in config.PROMPT_CHOICES]
    assert len(labels) == len(set(labels)), "duplicate labels"


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
            "1",  # provider: 1 = Anthropic (sorted: anthropic, google, openai, openai_compat)
            "claude-haiku-4-5",  # model (typed custom)
            "read the screen",  # prompt (typed custom)
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
    assert saved["provider"] == "anthropic"
    assert saved["api_key"] == "sk-ant-wizard-key"
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
            "1",  # provider: Anthropic
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
                "api_key": "old-key",
                "telegram_bot_token": "old-token",
                "telegram_chat_id": "old-chat",
                "save_dir": str(tmp_path),
                "prompt": "p",
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
            "1",  # provider: Anthropic
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
        ["Y", "3", "1", "claude-opus-5", "p", "k", "t", "c", str(tmp_path)]
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
                "api_key": "old-key",
                "telegram_bot_token": "old-token",
                "telegram_chat_id": "old-chat",
                "save_dir": str(tmp_path),
                "prompt": "p",
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
            "1",  # provider: Anthropic
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
    monkeypatch.setattr(vision, "verify_key", lambda cfg: (False, "key is invalid"))
    monkeypatch.setattr(notify, "verify_credentials", lambda token, chat: (True, "ok"))
    path = tmp_path / "config.json"
    scripted.extend(
        ["Y", "3", "1", "claude-opus-5", "p", "k", "t", "c", str(tmp_path)]
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
        "api_key": "old-key",
        "telegram_bot_token": "old-token",
        "telegram_chat_id": "old-chat",
        "save_dir": str(tmp_path),
        "prompt": "keep me",
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
        "api_key": "just-a-key",
    }
    path.write_text(json.dumps(partial), encoding="utf-8")

    picked = {"left": 10, "top": 20, "width": 300, "height": 200}
    assert (
        config.run_set_region(path, picker_factory=_picker_factory(picked)) == 0
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved.keys()) <= {"region", "api_key", "monitor"}
    assert saved["region"] == picked
    assert saved["api_key"] == "just-a-key"


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
                "api_key": "k",
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


def test_set_key_updates_only_the_key(tmp_path, answers):
    scripted, _ = answers
    path = tmp_path / "config.json"
    original = {
        "region": {"left": 50, "top": 60, "width": 400, "height": 300},
        "api_key": "old-key",
        "telegram_bot_token": "old-token",
        "telegram_chat_id": "old-chat",
        "save_dir": str(tmp_path),
        "prompt": "keep me",
        "dwell_seconds": 2.5,
        "model": "claude-opus-5",
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    scripted.append("sk-ant-new-key-value")

    assert config.run_set_key(path) == 0

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["api_key"] == "sk-ant-new-key-value"
    for key, value in original.items():
        if key == "api_key":
            continue
        assert saved[key] == value


def test_set_key_refuses_when_no_config_exists(tmp_path, capsys):
    path = tmp_path / "config.json"  # does not exist
    assert config.run_set_key(path) == 1
    err = capsys.readouterr().err
    assert "--configure" in err
    assert not path.exists()


def test_set_key_names_the_current_provider_on_prompt(tmp_path, answers, capsys):
    """--key should tell the user *which* provider it is prompting for, so a
    user who just switched via --model doesn't wonder which key to paste."""
    scripted, _ = answers
    path = tmp_path / "config.json"
    payload = _existing_config()
    payload["provider"] = "openai"
    payload["model"] = "gpt-5"
    path.write_text(json.dumps(payload), encoding="utf-8")
    scripted.append("sk-openai-new-key")

    assert config.run_set_key(path) == 0
    out = capsys.readouterr().out
    assert "OpenAI" in out  # display_name of the openai provider


def test_prompt_compat_endpoint_picks_preset_by_number(monkeypatch):
    """Numbered input '1' selects the first preset (verified by URL)."""
    scripted: list[str] = ["1"]

    def take(prompt=""):
        return scripted.pop(0) if scripted else ""

    monkeypatch.setattr("builtins.input", take)
    target: dict = {}
    default_model = config._prompt_compat_endpoint(target)
    first_label, (first_url, first_model, _note) = next(iter(config.COMPAT_PRESETS.items()))
    assert target["base_url"] == first_url
    assert target["model"] == first_model
    assert default_model == first_model


def test_prompt_compat_endpoint_recognises_preset_label(monkeypatch):
    """Typing 'deepseek' expands to the DeepSeek base URL + default model."""
    scripted: list[str] = ["deepseek"]

    def take(prompt=""):
        return scripted.pop(0) if scripted else ""

    monkeypatch.setattr("builtins.input", take)
    target: dict = {}
    default_model = config._prompt_compat_endpoint(target)
    deepseek_url, deepseek_model, _ = config.COMPAT_PRESETS["deepseek"]
    assert target["base_url"] == deepseek_url
    assert target["model"] == deepseek_model
    assert default_model == deepseek_model


def test_prompt_compat_endpoint_custom_url_leaves_model_alone(monkeypatch):
    """Typing an arbitrary URL sets base_url but returns None so the caller
    still asks for the model separately — a custom endpoint has no known
    default vision model."""
    scripted: list[str] = ["https://compat.example.com/v1"]

    def take(prompt=""):
        return scripted.pop(0) if scripted else ""

    monkeypatch.setattr("builtins.input", take)
    target: dict = {"model": "keep-me"}
    default_model = config._prompt_compat_endpoint(target)
    assert target["base_url"] == "https://compat.example.com/v1"
    assert target["model"] == "keep-me"
    assert default_model is None


def test_set_model_updates_only_the_model(tmp_path, answers):
    scripted, _ = answers
    path = tmp_path / "config.json"
    original = {
        "region": {"left": 50, "top": 60, "width": 400, "height": 300},
        "api_key": "old-key",
        "telegram_bot_token": "old-token",
        "telegram_chat_id": "old-chat",
        "save_dir": str(tmp_path),
        "prompt": "keep me",
        "dwell_seconds": 2.5,
        "model": "claude-opus-5",
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    # Provider: 1 = Anthropic (sorted), model preset 2 = claude-haiku-4-5.
    scripted.extend(["1", "2"])

    assert config.run_set_model(path) == 0

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["model"] == "claude-haiku-4-5"
    assert saved["provider"] == "anthropic"
    for key, value in original.items():
        if key == "model":
            continue
        assert saved[key] == value


def test_set_model_refuses_when_no_config_exists(tmp_path, capsys):
    path = tmp_path / "config.json"  # does not exist
    assert config.run_set_model(path) == 1
    err = capsys.readouterr().err
    assert "--configure" in err
    assert not path.exists()


def _existing_config():
    return {
        "region": {"left": 50, "top": 60, "width": 400, "height": 300},
        "api_key": "old-key",
        "telegram_bot_token": "old-token",
        "telegram_chat_id": "old-chat",
        "save_dir": "/keep/me",
        "prompt": "keep me",
        "dwell_seconds": 2.5,
        "model": "claude-opus-5",
    }


def test_set_prompt_updates_only_the_prompt(tmp_path, answers):
    scripted, _ = answers
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_existing_config()), encoding="utf-8")
    scripted.append("Find bugs in the screenshot.")

    assert config.run_set_prompt(path) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["prompt"] == "Find bugs in the screenshot."
    for key, value in _existing_config().items():
        if key == "prompt":
            continue
        assert saved[key] == value


def test_set_dwell_updates_only_dwell_seconds(tmp_path, answers):
    scripted, _ = answers
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_existing_config()), encoding="utf-8")
    scripted.append("4.5")

    assert config.run_set_dwell(path) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["dwell_seconds"] == 4.5
    for key, value in _existing_config().items():
        if key == "dwell_seconds":
            continue
        assert saved[key] == value


def test_set_save_dir_updates_only_save_dir(tmp_path, answers):
    scripted, _ = answers
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_existing_config()), encoding="utf-8")
    scripted.append("/new/place")

    assert config.run_set_save_dir(path) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["save_dir"] == "/new/place"
    for key, value in _existing_config().items():
        if key == "save_dir":
            continue
        assert saved[key] == value


def test_set_telegram_updates_both_token_and_chat_id_together(tmp_path, answers):
    scripted, _ = answers
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_existing_config()), encoding="utf-8")
    scripted.extend(["new-token", "new-chat"])

    assert config.run_set_telegram(path) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["telegram_bot_token"] == "new-token"
    assert saved["telegram_chat_id"] == "new-chat"
    for key, value in _existing_config().items():
        if key in ("telegram_bot_token", "telegram_chat_id"):
            continue
        assert saved[key] == value


@pytest.mark.parametrize(
    "setter",
    ["run_set_prompt", "run_set_dwell", "run_set_save_dir", "run_set_telegram"],
)
def test_single_field_setters_refuse_when_no_config_exists(tmp_path, capsys, setter):
    path = tmp_path / "config.json"  # does not exist
    assert getattr(config, setter)(path) == 1
    err = capsys.readouterr().err
    assert "--configure" in err
    assert not path.exists()


# --------------------------------------------------------------------------- #
# run_show — --show flag
# --------------------------------------------------------------------------- #


def test_show_prints_every_field_and_masks_credentials(tmp_path, capsys):
    path = tmp_path / "config.json"
    payload = _existing_config()
    payload["api_key"] = "sk-ant-a-very-long-secret-key-value"
    payload["telegram_bot_token"] = "123456:ABCDEFGHIJKLMNOP"
    payload["telegram_chat_id"] = "9876543210"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert config.run_show(path) == 0
    out = capsys.readouterr().out

    # Every field is named.
    for label in (
        "Region:",
        "Dwell:",
        "Provider:",
        "Model:",
        "Default prompt:",
        "Save directory:",
        "API key:",
        "Telegram bot:",
        "Telegram chat:",
    ):
        assert label in out
    # No secret appears in full — masking should truncate before the 8th char.
    assert "sk-ant-a-very-long" not in out
    assert "123456:ABCDEFGHIJKLMNOP" not in out
    assert "9876543210" not in out


def test_show_migrates_legacy_anthropic_api_key_for_display(tmp_path, capsys):
    """A legacy config with only anthropic_api_key still shows a masked API
    key line — merge_defaults migrates it into api_key on read."""
    path = tmp_path / "config.json"
    payload = _existing_config()
    del payload["api_key"]
    payload["anthropic_api_key"] = "sk-ant-legacy-full-secret"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert config.run_show(path) == 0
    out = capsys.readouterr().out
    assert "API key:" in out
    assert "sk-ant-legacy-full-secret" not in out
    # First eight characters (mask policy) still visible.
    assert "sk-ant-l" in out


def test_show_reports_a_hand_edited_unknown_provider_as_a_config_error(tmp_path, capsys):
    """A user who edits the config to `provider: "typo"` must see a crisp
    'Unknown provider' message, not the last-resort "Unexpected error:
    KeyError" that get_provider() would otherwise raise."""
    path = tmp_path / "config.json"
    payload = _existing_config()
    payload["provider"] = "not-a-real-provider"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert config.run_show(path) == 1
    err = capsys.readouterr().err
    assert "Unknown provider" in err
    assert "not-a-real-provider" in err


def test_set_key_reports_a_hand_edited_unknown_provider_as_a_config_error(tmp_path, capsys):
    path = tmp_path / "config.json"
    payload = _existing_config()
    payload["provider"] = "not-a-real-provider"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert config.run_set_key(path) == 1
    err = capsys.readouterr().err
    assert "Unknown provider" in err


def test_set_key_still_runs_with_openai_compat_missing_base_url(tmp_path, answers, capsys):
    """A user whose openai_compatible config is missing base_url must still
    be able to reach the API-key prompt without first fixing base_url — the
    up-front check gates only on unknown provider names, not on the full
    validator. The tail-validate still refuses to save until base_url is
    populated, but by then the user has learned exactly what remains
    broken (rather than being told to run --model when they tried to
    run --key)."""
    scripted, used = answers
    path = tmp_path / "config.json"
    payload = _existing_config()
    payload["provider"] = "openai_compatible"
    payload["base_url"] = ""  # deliberately broken
    payload["model"] = "deepseek-vl2"
    path.write_text(json.dumps(payload), encoding="utf-8")
    scripted.append("sk-new-key")

    result = config.run_set_key(path)

    # Setter reached the input prompt (the "sk-new-key" answer was consumed);
    # if the up-front check had aborted, no input() call would have happened.
    assert scripted == []
    assert used, "setter aborted before prompting for the key"

    # Tail-validate refuses the save because base_url is still empty, so
    # the file on disk is unchanged — the key the user just typed does
    # not land in a config that would still route captures nowhere.
    assert result == 1
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["api_key"] == "old-key"
    # The failure message names the field that is still wrong so the user
    # knows to run --model next.
    assert "base_url" in capsys.readouterr().err


def test_set_model_still_runs_with_openai_compat_missing_base_url(tmp_path, answers):
    """Same as the --key case: --model must be reachable when it is the
    tool the user needs to fix a missing base_url."""
    scripted, _ = answers
    path = tmp_path / "config.json"
    payload = _existing_config()
    payload["provider"] = "openai_compatible"
    payload["base_url"] = ""
    path.write_text(json.dumps(payload), encoding="utf-8")
    # provider: keep current (openai_compatible is index 4 out of 4 known)
    # then pick preset "1" for the compat-endpoint prompt (DeepSeek).
    scripted.extend(["4", "1"])

    assert config.run_set_model(path) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["provider"] == "openai_compatible"
    assert saved["base_url"]  # now populated by the preset
    assert saved["model"]


def test_show_refuses_when_no_config_exists(tmp_path, capsys):
    path = tmp_path / "config.json"  # does not exist
    assert config.run_show(path) == 1
    err = capsys.readouterr().err
    assert "--configure" in err
    assert not path.exists()


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
                "api_key": "k",
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
