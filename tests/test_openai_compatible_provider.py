"""OpenAI-compatible provider — required base_url + preset URLs."""

from __future__ import annotations

from screenrecon.config import COMPAT_PRESETS
from screenrecon.providers.openai_compatible import OpenAICompatibleProvider


def test_missing_base_url_fails_verification_without_hitting_network():
    """No base_url means we cannot even build the client — the probe must
    fail cleanly instead of contacting api.openai.com by default."""
    provider = OpenAICompatibleProvider()
    ok, message = provider.verify_key({"model": "deepseek-vl2", "api_key": "sk-x"})
    assert ok is False
    assert "base URL" in message or "base_url" in message


def test_missing_base_url_fails_ask_streaming_without_hitting_network():
    provider = OpenAICompatibleProvider()
    reply = provider.ask_streaming(
        {"model": "deepseek-vl2", "api_key": "sk-x"}, [], lambda _: None
    )
    assert reply.ok is False
    assert "base_url" in reply.text.lower() or "base url" in reply.text.lower()


def test_preset_base_urls_include_the_three_supported_chinese_providers():
    """Wizard shortcuts (see SR-23 issue) must at minimum offer DeepSeek,
    Kimi (Moonshot), and Doubao — the three named in the design."""
    assert "deepseek" in COMPAT_PRESETS
    assert "kimi" in COMPAT_PRESETS or "moonshot" in COMPAT_PRESETS
    assert "doubao" in COMPAT_PRESETS
    # Every preset must be a full https URL, not a bare host, and carry
    # a default model ID so the wizard has something to pre-fill.
    for label, (url, default_model, _note) in COMPAT_PRESETS.items():
        assert url.startswith("https://"), f"{label} preset must be https://"
        assert default_model, f"{label} preset must include a default model"


def test_openai_compatible_is_never_inferred_from_model_prefix():
    """The three Chinese providers use model IDs like `deepseek-vl2` that no
    prefix rule can distinguish from a hypothetical OpenAI model. The
    dispatcher therefore only picks openai_compatible when the user set
    `provider: "openai_compatible"` explicitly — a `deepseek-vl2` model
    with no explicit provider falls back to the default (anthropic)
    rather than accidentally routing to the compat provider."""
    from screenrecon import vision

    assert vision.get_provider({"model": "deepseek-vl2"}).name == "anthropic"
    # gpt-* stays with real OpenAI, not the compat path.
    assert vision.get_provider({"model": "gpt-4o"}).name == "openai"


def test_explicit_provider_field_selects_openai_compatible():
    from screenrecon import vision

    provider = vision.get_provider(
        {"provider": "openai_compatible", "model": "anything"}
    )
    assert provider.name == "openai_compatible"
