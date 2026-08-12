"""Telegram delivery (design doc 5.5).

Failures print a warning only — no retries, and the watch loop is never
interrupted (v1). Nothing in this module raises.

Anything printed from a third party first passes through :func:`_sanitize`:
requests embeds the full URL in its exceptions, and that URL contains the bot
token — including in percent-encoded form, which a literal search would miss.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from . import ui

API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 30

CAPTION_LIMIT = 1024
"""Telegram's sendPhoto caption limit, in UTF-16 code units."""

MESSAGE_LIMIT = 4096
"""Telegram's sendMessage body limit, in UTF-16 code units."""

_TRUNCATE_MARK = "... (full text in the next message)"

CAPTION_TRUNCATE_AT = CAPTION_LIMIT - len(_TRUNCATE_MARK)
"""Budget for the answer itself. Derived from the limit and the marker so that
caption + marker can never exceed CAPTION_LIMIT."""

_BOT_PATH = re.compile(r"/bot[^/\s]+", re.IGNORECASE)
"""Matches the token segment of a Bot API URL however it happens to be encoded."""


# --------------------------------------------------------------------------- #
# Length accounting
# --------------------------------------------------------------------------- #


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — the unit Telegram counts its limits in.

    Characters outside the BMP (emoji, rarer CJK ideographs) count as two, so
    counting Python characters would let a valid-looking caption be rejected.
    """
    return len(text.encode("utf-16-le")) // 2


def _truncate_utf16(text: str, budget: int) -> str:
    """Longest prefix of ``text`` that fits in ``budget`` UTF-16 code units."""
    if utf16_len(text) <= budget:
        return text
    end = min(len(text), budget)
    while end > 0 and utf16_len(text[:end]) > budget:
        end -= 1
    return text[:end]


# --------------------------------------------------------------------------- #
# Pure splitting logic (unit-testable)
# --------------------------------------------------------------------------- #


def split_caption(text: str) -> tuple[str, str | None]:
    """Split text against the caption limit.

    Returns ``(caption, followup)``; ``followup`` is None when no extra text
    message is needed.
    """
    text = text or ""
    if utf16_len(text) <= CAPTION_LIMIT:
        return text, None
    return _truncate_utf16(text, CAPTION_TRUNCATE_AT) + _TRUNCATE_MARK, text


def chunk_text(text: str, size: int = MESSAGE_LIMIT) -> list[str]:
    """Split long text into chunks of at most ``size`` UTF-16 code units."""
    if size <= 0:
        raise ValueError("size must be a positive integer")
    chunks: list[str] = []
    rest = text or ""
    while rest:
        piece = _truncate_utf16(rest, size) or rest[:1]
        chunks.append(piece)
        rest = rest[len(piece) :]
    return chunks


# --------------------------------------------------------------------------- #
# Network calls
# --------------------------------------------------------------------------- #


def _endpoint(token: str, method: str) -> str:
    return f"{API_BASE}/bot{token}/{method}"


def _sanitize(text: str, token: str, chat_id: str | None = None) -> str:
    """Remove the bot token and chat ID from third-party text, in any encoding.

    The literal scrub runs first: it is the only pass that can match a token
    containing a ``/``, which the URL-path regex would otherwise split into
    pieces that neither pass can then recognise. The regex runs afterwards as a
    backstop for encodings we did not anticipate.
    """
    scrubbed = ui.scrub(str(text), [token, quote(token, safe=""), chat_id])
    return _BOT_PATH.sub("/bot<redacted>", scrubbed)


def _call(
    token: str, method: str, chat_id: str | None = None, **kwargs: Any
) -> tuple[bool, str, Any]:
    """Call the Bot API. Returns (ok, message, result). Never raises."""
    try:
        import requests
    except ImportError:
        return False, "Missing dependency 'requests'. Install with: pip install screenrecon", None

    try:
        response = requests.post(_endpoint(token, method), timeout=TIMEOUT_SECONDS, **kwargs)
    except Exception as exc:  # any requests transport error
        detail = _sanitize(f"request failed: {type(exc).__name__}: {exc}", token, chat_id)
        return False, detail, None

    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        return False, f"Telegram returned an unparseable response (HTTP {status}).", None

    # A captive portal or proxy can return valid JSON that is not an object.
    if not isinstance(payload, dict):
        return False, f"Telegram returned an unexpected response (HTTP {status}).", None

    if payload.get("ok"):
        return True, "ok", payload.get("result")

    description = str(payload.get("description", "unknown error"))
    code = payload.get("error_code", response.status_code)
    return False, _sanitize(f"Telegram error {code}: {description}", token, chat_id), None


def send(bot_token: str, chat_id: str, png_bytes: bytes, text: str) -> bool:
    """Send the screenshot plus text. Returns whether everything succeeded."""
    caption, followup = split_caption(text)

    photo_ok, message, _ = _call(
        bot_token,
        "sendPhoto",
        chat_id=chat_id,
        data={"chat_id": chat_id, "caption": caption},
        files={"photo": ("screenrecon.png", png_bytes, "image/png")},
    )
    if not photo_ok:
        ui.warn(f"Telegram photo delivery failed: {message}")
        # The photo and the text fail independently (an oversized image is
        # rejected while the text is fine), so still deliver the answer.
        followup = text or None

    if not followup:
        if photo_ok:
            ui.info("Sent to Telegram.")
        return photo_ok

    text_ok = True
    chunks = chunk_text(followup)
    for index, chunk in enumerate(chunks, start=1):
        ok, message, _ = _call(
            bot_token, "sendMessage", chat_id=chat_id, data={"chat_id": chat_id, "text": chunk}
        )
        if not ok:
            ui.warn(f"Telegram text delivery failed (part {index}/{len(chunks)}): {message}")
            text_ok = False
    if photo_ok and text_ok:
        ui.info(f"Sent to Telegram (photo + {len(chunks)} text part(s)).")
    elif text_ok:
        ui.info(f"Sent the answer to Telegram as text ({len(chunks)} part(s)); the photo failed.")
    return photo_ok and text_ok


def verify_credentials(bot_token: str, chat_id: str) -> tuple[bool, str]:
    """Setup wizard: getMe validates the token, a test message validates the chat ID (FR-11)."""
    bot_token = (bot_token or "").strip()
    chat_id = (chat_id or "").strip()
    if not bot_token:
        return False, "No bot token entered."
    if not chat_id:
        return False, "No chat ID entered."

    ok, message, result = _call(bot_token, "getMe", chat_id=chat_id)
    if not ok:
        return False, f"bot token check failed: {message}"
    username = ""
    if isinstance(result, dict) and result.get("username"):
        username = f" (@{result['username']})"

    ok, message, _ = _call(
        bot_token,
        "sendMessage",
        chat_id=chat_id,
        data={
            "chat_id": chat_id,
            "text": "ScreenRecon is configured. This message verifies the delivery channel.",
        },
    )
    if not ok:
        return False, f"token{username} is valid, but sending to that chat ID failed: {message}"
    return True, f"bot{username} reached chat {ui.mask(chat_id)}; a test message was sent."
