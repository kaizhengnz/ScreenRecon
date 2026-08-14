"""OpenAI provider (design doc 5.4).

Wraps the ``openai`` SDK. Reads ``cfg["api_key"]`` and ``cfg["model"]``;
subclasses (see :mod:`.openai_compatible`) may additionally read
``cfg["base_url"]`` to point the client at a compatible endpoint.

The Chat Completions message format is different from Anthropic's — image
parts arrive as ``{"type": "image_url", "image_url": {"url": "data:..."}}`` —
so ``_messages_from_turns`` builds that shape, and ``_stream_text`` walks
the chunk-by-chunk delta stream that ``chat.completions.create(stream=True)``
yields.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .. import ui
from ..vision import MAX_TOKENS, Reply, Turn


class OpenAIProvider:
    name = "openai"
    display_name = "OpenAI (GPT)"

    def _api_key(self, cfg: Mapping[str, Any]) -> str:
        return str(cfg.get("api_key") or "").strip()

    def _client(self, cfg: Mapping[str, Any]):
        """Construct the OpenAI SDK client. Subclasses can override to inject
        a ``base_url`` (see :class:`OpenAICompatibleProvider`)."""
        import openai

        return openai.OpenAI(api_key=self._api_key(cfg))

    def ask_streaming(
        self,
        cfg: Mapping[str, Any],
        turns: Sequence[Turn],
        on_delta: Callable[[str], None],
    ) -> Reply:
        api_key = self._api_key(cfg)
        model = str(cfg.get("model") or "")
        try:
            import openai  # noqa: F401 - trigger the ImportError early
        except ImportError:
            return Reply(
                False,
                "Missing dependency 'openai'. Install with: pip install 'screenrecon[openai]'",
            )

        try:
            client = self._client(cfg)
        except Exception as exc:
            return Reply(False, translate_error(exc, [api_key]))

        # OpenAI reasoning models (gpt-5 / o1 / o3 / o4) reject the classic
        # ``max_tokens`` and require ``max_completion_tokens``. Added to the
        # SDK in 1.45; ``pyproject`` pins that as the floor. The TypeError
        # fallback below covers a user who somehow ended up with 1.40-1.44
        # installed (an old venv layered under this package) — same
        # probe-and-retry shape AnthropicProvider uses for ``output_config``.
        request = {
            "model": model,
            "messages": _messages_from_turns(turns),
            "max_completion_tokens": MAX_TOKENS,
            "stream": True,
        }
        try:
            stream = client.chat.completions.create(**request)
        except TypeError as exc:
            if "max_completion_tokens" in str(exc):
                request.pop("max_completion_tokens")
                request["max_tokens"] = MAX_TOKENS
                try:
                    stream = client.chat.completions.create(**request)
                except Exception as retry_exc:
                    return Reply(False, translate_error(retry_exc, [api_key]))
            else:
                return Reply(False, translate_error(exc, [api_key]))
        except Exception as exc:
            return Reply(False, translate_error(exc, [api_key]))

        chunks: list[str] = []
        try:
            for event in stream:
                delta = _delta_text(event)
                if delta:
                    chunks.append(delta)
                    on_delta(delta)
        except Exception as exc:
            return Reply(False, translate_error(exc, [api_key]))

        text = "".join(chunks)
        if not text:
            return Reply(False, "The AI returned an empty answer. Please retry.")
        return Reply(True, text)

    def verify_key(self, cfg: Mapping[str, Any]) -> tuple[bool, str]:
        api_key = self._api_key(cfg)
        model = str(cfg.get("model") or "").strip()
        if not api_key:
            return False, "No API key entered."
        try:
            import openai  # noqa: F401 - probe availability before making the SDK call
        except ImportError:
            return (
                False,
                "Missing dependency 'openai'. "
                "Install with: pip install 'screenrecon[openai]'",
            )
        try:
            self._client(cfg).models.retrieve(model)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 404:
                return (
                    False,
                    f"Key works, but model {model!r} does not exist. Check the model name.",
                )
            return False, translate_error(exc, [api_key])
        return True, f"Key is valid and model {model} is available."


# --------------------------------------------------------------------------- #
# Message-format conversion
# --------------------------------------------------------------------------- #


def image_part(image_bytes: bytes) -> dict[str, Any]:
    """Wrap JPEG bytes in an OpenAI Chat Completions image_url part."""
    encoded = base64.standard_b64encode(image_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
    }


def _messages_from_turns(turns: Sequence[Turn]) -> list[dict[str, Any]]:
    """Translate provider-agnostic Turns into Chat Completions messages.

    Assistant turns collapse to plain-string content (OpenAI accepts either
    a string or a parts list for assistant messages; string is simpler).
    User turns become a parts list so the image can precede the text.
    """
    messages: list[dict[str, Any]] = []
    for turn in turns:
        if turn.role == "assistant":
            messages.append({"role": "assistant", "content": turn.text})
            continue
        parts: list[dict[str, Any]] = []
        if turn.image is not None:
            parts.append(image_part(turn.image))
        parts.append({"type": "text", "text": turn.text})
        messages.append({"role": turn.role, "content": parts})
    return messages


def _delta_text(event: Any) -> str:
    """Pull the text out of one Chat Completions streaming event.

    Guarded because ``choices`` can be empty (usage-only trailer events) and
    the delta object shape drifts across SDK versions; ``getattr`` cascades
    tolerate both dict-shaped and object-shaped chunks.
    """
    choices = getattr(event, "choices", None) or []
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return ""
    content = getattr(delta, "content", None)
    return content or ""


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #


def translate_error(exc: Exception, secrets: Iterable[str | None] = ()) -> str:
    """Translate an OpenAI SDK exception into a readable message.

    Falls through to the scrubbed generic string if ``openai`` is not
    importable or the exception isn't one of the SDK's classes — the watch
    loop keeps running regardless.
    """
    try:
        import openai
    except ImportError:  # pragma: no cover - only when the dependency is missing
        return ui.scrub(f"AI API call failed: {exc}", secrets)

    if isinstance(exc, openai.AuthenticationError):
        return (
            "The API key is invalid or revoked. "
            "Run 'screenrecon --key' to set it again."
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return "This API key is not allowed to access that resource. Check its permissions."
    if isinstance(exc, openai.RateLimitError):
        return "Too many requests. Please try again shortly."
    if isinstance(exc, openai.NotFoundError):
        return "Model or endpoint not found. Check the 'model' field in your config."
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", "unknown")
        return f"The AI API returned {status}. Check your account credit or retry later."
    if isinstance(exc, openai.APITimeoutError):
        return "The AI API request timed out. Check your network and retry."
    if isinstance(exc, openai.APIConnectionError):
        return "Network connection failed. Check your internet connection."
    return ui.scrub(f"AI API call failed: {type(exc).__name__}: {exc}", secrets)
