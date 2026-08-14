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
