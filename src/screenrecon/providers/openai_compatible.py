"""OpenAI-compatible provider (design doc 5.4).

One code path covers Chinese endpoints (DeepSeek, Moonshot / Kimi, Doubao)
plus any other service that speaks Chat Completions. The only difference
from :class:`OpenAIProvider` is that ``cfg["base_url"]`` is required and
passed through when building the SDK client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .openai import OpenAIProvider

PRESET_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "kimi": "https://api.moonshot.cn/v1",   # alias
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
}
"""Wizard shortcuts: a user picks one of these labels and the base URL is
pre-filled. Values verified against each provider's compat-mode docs at
the time of writing; users can still type any custom URL."""


class OpenAICompatibleProvider(OpenAIProvider):
    name = "openai_compatible"
    display_name = "OpenAI-compatible (DeepSeek / Kimi / Doubao / custom)"

    def _client(self, cfg: Mapping[str, Any]):
        import openai

        base_url = str(cfg.get("base_url") or "").strip()
        if not base_url:
            raise ValueError(
                "openai_compatible provider requires 'base_url' in config. "
                "Set it via 'screenrecon --configure' or edit the config file."
            )
        return openai.OpenAI(api_key=self._api_key(cfg), base_url=base_url)

    def verify_key(self, cfg: Mapping[str, Any]) -> tuple[bool, str]:
        base_url = str(cfg.get("base_url") or "").strip()
        if not base_url:
            return False, "No base URL configured for the OpenAI-compatible endpoint."
        return super().verify_key(cfg)
