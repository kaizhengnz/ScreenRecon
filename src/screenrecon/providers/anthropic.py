"""Anthropic Claude provider (design doc 5.4).

Wraps the ``anthropic`` SDK. Converts :class:`Turn` sequences into Anthropic
message dicts, streams via ``client.messages.stream``, and translates
``anthropic.*Error`` exceptions into readable ``Reply.text``.

Module-level ``translate_error`` and ``image_block`` remain accessible for
tests and diagnostics — they were the previous public surface, and pinning
their shape stops regressions when the provider layer moves around.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .. import ui
from ..vision import EFFORT, MAX_TOKENS, Reply, Turn

_effort_unsupported: set[str] = set()
"""Models known to reject ``output_config.effort``. Cached per process so we
only pay the parameter-error probe once per model."""


class AnthropicProvider:
    name = "anthropic"
    display_name = "Anthropic (Claude)"

    def ask_streaming(
        self,
        cfg: Mapping[str, Any],
        turns: Sequence[Turn],
        on_delta: Callable[[str], None],
    ) -> Reply:
        api_key = str(cfg.get("anthropic_api_key") or cfg.get("api_key") or "")
        model = str(cfg.get("model") or "")
        try:
            import anthropic
        except ImportError:
            return Reply(
                False,
                "Missing dependency 'anthropic'. Install with: pip install screenrecon",
            )

        try:
            client = anthropic.Anthropic(api_key=api_key)
        except Exception as exc:
            return Reply(False, translate_error(exc, [api_key]))

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": _messages_from_turns(turns),
        }
        use_effort = model not in _effort_unsupported

        for attempt in range(2):
            request = dict(payload)
            if use_effort:
                request["output_config"] = {"effort": EFFORT}
            chunks: list[str] = []
            try:
                with client.messages.stream(**request) as stream:
                    for chunk in stream.text_stream:
                        chunks.append(chunk)
                        on_delta(chunk)
                    final = stream.get_final_message()
            except Exception as exc:
                if use_effort and attempt == 0 and _is_parameter_error(exc):
                    _effort_unsupported.add(model)
                    use_effort = False
                    continue
                return Reply(False, translate_error(exc, [api_key]))

            text = "".join(chunks) or _extract_text(final)
            if not text:
                reason = getattr(final, "stop_reason", None)
                if reason == "refusal":
                    return Reply(
                        False,
                        "The AI declined to answer this request (safety policy).",
                    )
                if reason == "max_tokens":
                    return Reply(
                        False,
                        "The answer hit the output limit. "
                        "Try a smaller region or a shorter prompt.",
                    )
                return Reply(False, "The AI returned an empty answer. Please retry.")
            return Reply(True, text)

        return Reply(False, "The AI API call failed. Please retry.")

    def verify_key(self, cfg: Mapping[str, Any]) -> tuple[bool, str]:
        api_key = str(cfg.get("anthropic_api_key") or cfg.get("api_key") or "").strip()
        model = str(cfg.get("model") or "").strip()
        if not api_key:
            return False, "No API key entered."
        try:
            import anthropic
        except ImportError:
            return False, "Missing dependency 'anthropic'; cannot verify."
        try:
            anthropic.Anthropic(api_key=api_key).models.retrieve(model)
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


def image_block(image_bytes: bytes) -> dict[str, Any]:
    """Wrap JPEG bytes in an Anthropic image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(image_bytes).decode("ascii"),
        },
    }


def _messages_from_turns(turns: Sequence[Turn]) -> list[dict[str, Any]]:
    """Translate provider-agnostic Turns into Anthropic's messages array."""
    messages: list[dict[str, Any]] = []
    for turn in turns:
        content: list[dict[str, Any]] = []
        if turn.image is not None:
            content.append(image_block(turn.image))
        if turn.text or not content:
            # Anthropic requires at least one content block per message; text
            # backfills for the (currently unused) image-only turn shape.
            content.append({"type": "text", "text": turn.text})
        messages.append({"role": turn.role, "content": content})
    return messages


def _extract_text(message: Any) -> str:
    """Join every content block whose type is ``"text"``.

    Kept as a module function so ``get_final_message()`` fallbacks after an
    empty ``text_stream`` still produce readable output — some SDK versions
    materialise text into the final message even when it never fires as a
    delta.
    """
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    joined = "\n".join(part for part in parts if part.strip())
    return joined.strip()


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #


def _is_parameter_error(exc: Exception) -> bool:
    """True when the failure is 'this parameter is not accepted' rather than
    a real error. Two shapes: a 400 from the API rejecting the parameter for
    this model, or a ``TypeError`` from an SDK older than the parameter.
    Both are recoverable by resending without ``output_config``.
    """
    text = str(exc).lower()
    if isinstance(exc, TypeError):
        return "output_config" in text or "effort" in text
    if getattr(exc, "status_code", None) != 400:
        return False
    return "effort" in text or "output_config" in text


def translate_error(exc: Exception, secrets: Iterable[str | None] = ()) -> str:
    """Translate an Anthropic SDK exception into a readable message,
    with any secrets in the exception text scrubbed."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - only when the dependency is missing
        return ui.scrub(f"AI API call failed: {exc}", secrets)

    if isinstance(exc, anthropic.AuthenticationError):
        return (
            "The API key is invalid or revoked. "
            "Run 'screenrecon --configure' to set it again."
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key is not allowed to access that resource. Check its permissions."
    if isinstance(exc, anthropic.RateLimitError):
        return "Too many requests. Please try again shortly."
    if isinstance(exc, anthropic.NotFoundError):
        return "Model or endpoint not found. Check the 'model' field in your config."
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", "unknown")
        return f"The AI API returned {status}. Check your account credit or retry later."
    if isinstance(exc, anthropic.APITimeoutError):
        return "The AI API request timed out. Check your network and retry."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Network connection failed. Check your internet connection."
    return ui.scrub(f"AI API call failed: {type(exc).__name__}: {exc}", secrets)
