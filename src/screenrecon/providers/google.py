"""Google Gemini provider (design doc 5.4).

Wraps the ``google-genai`` SDK. Contents alternate between user and model
turns; an image is a ``types.Part.from_bytes`` beside the text. Streaming
comes back as an iterable of chunks each carrying incremental text.

Verify uses ``client.models.get(...)`` — no cheaper single-model probe is
in the SDK yet, but ``get`` is a metadata lookup, not a generation call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .. import ui
from ..vision import Reply, Turn


class GoogleProvider:
    name = "google"
    display_name = "Google (Gemini)"

    def _api_key(self, cfg: Mapping[str, Any]) -> str:
        return str(cfg.get("api_key") or "").strip()

    def _client(self, cfg: Mapping[str, Any]):
        from google import genai

        return genai.Client(api_key=self._api_key(cfg))

    def ask_streaming(
        self,
        cfg: Mapping[str, Any],
        turns: Sequence[Turn],
        on_delta: Callable[[str], None],
    ) -> Reply:
        api_key = self._api_key(cfg)
        model = str(cfg.get("model") or "")
        try:
            from google import genai  # noqa: F401 - trigger the ImportError early
        except ImportError:
            return Reply(
                False,
                "Missing dependency 'google-genai'. Install with: pip install google-genai",
            )

        try:
            client = self._client(cfg)
        except Exception as exc:
            return Reply(False, translate_error(exc, [api_key]))

        try:
            contents = _contents_from_turns(turns)
        except Exception as exc:
            return Reply(False, translate_error(exc, [api_key]))

        chunks: list[str] = []
        try:
            for chunk in client.models.generate_content_stream(
                model=model,
                contents=contents,
            ):
                delta = getattr(chunk, "text", None)
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
            from google import genai  # noqa: F401
        except ImportError:
            return False, "Missing dependency 'google-genai'; cannot verify."
        try:
            self._client(cfg).models.get(model=model)
        except Exception as exc:
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if status == 404:
                return (
                    False,
                    f"Key works, but model {model!r} does not exist. Check the model name.",
                )
            return False, translate_error(exc, [api_key])
        return True, f"Key is valid and model {model} is available."


# --------------------------------------------------------------------------- #
# Content-format conversion
# --------------------------------------------------------------------------- #


def _contents_from_turns(turns: Sequence[Turn]) -> list[Any]:
    """Translate provider-agnostic Turns into Gemini's ``contents`` list.

    Gemini uses ``role="user"`` and ``role="model"`` — assistant maps to
    the latter. Each user turn becomes a Content with parts; assistant
    turns collapse to plain text (Gemini accepts a bare string entry in
    contents, which the SDK wraps).
    """
    from google.genai import types

    contents: list[Any] = []
    for turn in turns:
        if turn.role == "assistant":
            contents.append(types.Content(role="model", parts=[types.Part(text=turn.text)]))
            continue
        parts: list[Any] = []
        if turn.image is not None:
            parts.append(types.Part.from_bytes(data=turn.image, mime_type="image/jpeg"))
        parts.append(types.Part(text=turn.text))
        contents.append(types.Content(role="user", parts=parts))
    return contents


# --------------------------------------------------------------------------- #
# Error translation
# --------------------------------------------------------------------------- #


def translate_error(exc: Exception, secrets: Iterable[str | None] = ()) -> str:
    """Translate a google-genai exception into a readable message.

    The SDK exposes a narrower exception taxonomy than Anthropic / OpenAI —
    most errors surface as ``google.genai.errors.APIError`` variants carrying
    an HTTP status code. Match on status where possible; fall through to the
    scrubbed generic string.
    """
    text = str(exc)
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    lower = text.lower()

    if status in (401, 403) or "api key" in lower or "unauthorized" in lower:
        return (
            "The API key is invalid or revoked. "
            "Run 'screenrecon --key' to set it again."
        )
    if status == 429 or "rate" in lower and "limit" in lower:
        return "Too many requests. Please try again shortly."
    if status == 404 or "not found" in lower:
        return "Model or endpoint not found. Check the 'model' field in your config."
    if status and 500 <= int(status) < 600:
        return f"The AI API returned {status}. Check your account credit or retry later."
    if "timeout" in lower or "timed out" in lower:
        return "The AI API request timed out. Check your network and retry."
    if "connection" in lower or "network" in lower:
        return "Network connection failed. Check your internet connection."
    return ui.scrub(f"AI API call failed: {type(exc).__name__}: {exc}", secrets)
