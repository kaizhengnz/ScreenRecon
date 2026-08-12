"""AI calls and error translation (design doc 5.4).

The ``ask*`` functions never raise: every failure is translated into a readable
message and returned, so the watch loop keeps running (NFR-2).
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from . import ui

MAX_TOKENS = 4096
"""Covers thinking plus the visible answer. Current Opus models run adaptive
thinking by default and this cap applies to both, so a tight budget can be spent
entirely on thinking and return no text at all."""

EFFORT = "low"
"""Reading a screenshot is a light task; low effort keeps latency and cost down.
Models that reject the parameter are downgraded automatically."""

_effort_unsupported: set[str] = set()
"""Models known to reject output_config.effort, so we only pay for one probe."""


@dataclass(frozen=True)
class Reply:
    """One AI call. When ``ok`` is False, ``text`` is a readable error message."""

    ok: bool
    text: str


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #


def image_block(png_bytes: bytes) -> dict[str, Any]:
    """Wrap PNG bytes in an AI image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png_bytes).decode("ascii"),
        },
    }


def user_turn(png_bytes: bytes | None, text: str) -> dict[str, Any]:
    """Build a user message with an optional image followed by text."""
    content: list[dict[str, Any]] = []
    if png_bytes is not None:
        content.append(image_block(png_bytes))
    content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}


def _extract_text(message: Any) -> str:
    """Join every content block whose type is "text" (design doc 5.4)."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(str(getattr(block, "text", "")))
    joined = "\n".join(part for part in parts if part.strip())
    return joined.strip()


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


def ask(api_key: str, model: str, messages: Sequence[dict[str, Any]]) -> Reply:
    """Send a (possibly multi-turn) request to the AI. Never raises."""
    try:
        import anthropic
    except ImportError:
        return Reply(False, "Missing dependency 'anthropic'. Install with: pip install screenrecon")

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as exc:
        return Reply(False, translate_error(exc, [api_key]))

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": list(messages),
    }
    use_effort = model not in _effort_unsupported

    for attempt in range(2):
        request = dict(payload)
        if use_effort:
            request["output_config"] = {"effort": EFFORT}
        try:
            message = client.messages.create(**request)
        except Exception as exc:
            if use_effort and attempt == 0 and _is_parameter_error(exc):
                # This model does not accept output_config.effort — retry without it.
                _effort_unsupported.add(model)
                use_effort = False
                continue
            return Reply(False, translate_error(exc, [api_key]))

        text = _extract_text(message)
        if not text:
            reason = getattr(message, "stop_reason", None)
            if reason == "refusal":
                return Reply(False, "The AI declined to answer this request (safety policy).")
            if reason == "max_tokens":
                return Reply(
                    False,
                    "The answer hit the output limit. Try a smaller region or a shorter prompt.",
                )
            return Reply(False, "The AI returned an empty answer. Please retry.")
        return Reply(True, text)

    return Reply(False, "The AI API call failed. Please retry.")


def ask_image(api_key: str, model: str, png_bytes: bytes, prompt: str) -> Reply:
    """Single-turn question about one screenshot."""
    return ask(api_key, model, [user_turn(png_bytes, prompt)])


def verify_key(api_key: str, model: str) -> tuple[bool, str]:
    """Setup wizard: validate the key and model name with a minimal request (FR-11)."""
    api_key = (api_key or "").strip()
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
            return False, f"Key works, but model {model!r} does not exist. Check the model name."
        return False, translate_error(exc, [api_key])
    return True, f"Key is valid and model {model} is available."


# --------------------------------------------------------------------------- #
# Error translation (design doc 5.4)
# --------------------------------------------------------------------------- #


def _is_parameter_error(exc: Exception) -> bool:
    """True when the failure is "this parameter is not accepted", not a real error.

    Two shapes: the API rejecting the parameter for this model with a 400, and an
    SDK older than the parameter raising TypeError for an unexpected keyword.
    Both are recoverable by resending without ``output_config``.
    """
    text = str(exc).lower()
    if isinstance(exc, TypeError):
        return "output_config" in text or "effort" in text
    if getattr(exc, "status_code", None) != 400:
        return False
    return "effort" in text or "output_config" in text


def translate_error(exc: Exception, secrets: Iterable[str | None] = ()) -> str:
    """Translate an SDK exception into a readable message, with credentials masked."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - only when the dependency is missing
        return ui.scrub(f"AI API call failed: {exc}", secrets)

    if isinstance(exc, anthropic.AuthenticationError):
        return "The API key is invalid or revoked. Run 'screenrecon --configure' to set it again."
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
