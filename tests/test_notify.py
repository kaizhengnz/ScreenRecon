"""Telegram caption splitting and token hygiene (design doc 8.1)."""

from __future__ import annotations

import sys

import pytest

from screenrecon import notify

TOKEN = "123456:AAHfake-telegram-bot-token"
CHAT_ID = "987654321"


# --------------------------------------------------------------------------- #
# Caption splitting (design doc 5.5)
# --------------------------------------------------------------------------- #


def test_short_text_fits_in_the_caption():
    caption, followup = notify.split_caption("hello")
    assert caption == "hello"
    assert followup is None


def test_text_at_the_limit_still_fits():
    text = "x" * notify.CAPTION_LIMIT
    caption, followup = notify.split_caption(text)
    assert caption == text
    assert followup is None


def test_over_long_text_is_truncated_and_resent_in_full():
    text = "y" * (notify.CAPTION_LIMIT + 1)
    caption, followup = notify.split_caption(text)

    assert len(caption) <= notify.CAPTION_LIMIT
    assert caption.startswith("y" * notify.CAPTION_TRUNCATE_AT)
    assert caption.endswith("next message)")
    assert followup == text  # the follow-up carries the complete text, not the remainder


def test_empty_text_is_handled():
    assert notify.split_caption("") == ("", None)


# --------------------------------------------------------------------------- #
# Message chunking
# --------------------------------------------------------------------------- #


def test_chunking_respects_the_message_limit():
    text = "z" * (notify.MESSAGE_LIMIT * 2 + 17)
    chunks = notify.chunk_text(text)
    assert len(chunks) == 3
    assert all(len(chunk) <= notify.MESSAGE_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_chunking_empty_text_returns_nothing():
    assert notify.chunk_text("") == []


def test_chunking_rejects_non_positive_size():
    with pytest.raises(ValueError):
        notify.chunk_text("abc", 0)


# --------------------------------------------------------------------------- #
# Delivery behaviour
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def install_fake_requests(monkeypatch, responses):
    """Replace the requests module notify imports with a recording fake."""
    calls: list[dict] = []
    queue = list(responses)

    class FakeRequests:
        @staticmethod
        def post(url, timeout=None, **kwargs):
            calls.append({"url": url, "timeout": timeout, **kwargs})
            if isinstance(queue[0], Exception):
                raise queue.pop(0)
            return queue.pop(0)

    monkeypatch.setitem(sys.modules, "requests", FakeRequests)
    return calls


def test_short_text_sends_one_photo_only(monkeypatch):
    calls = install_fake_requests(monkeypatch, [FakeResponse({"ok": True})])
    assert notify.send(TOKEN, CHAT_ID, b"png-bytes", "short answer") is True
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/sendPhoto")
    assert calls[0]["data"]["caption"] == "short answer"
    assert calls[0]["timeout"] == notify.TIMEOUT_SECONDS


def test_long_text_sends_photo_then_full_text(monkeypatch):
    long_text = "w" * (notify.CAPTION_LIMIT + 500)
    calls = install_fake_requests(
        monkeypatch, [FakeResponse({"ok": True}), FakeResponse({"ok": True})]
    )
    assert notify.send(TOKEN, CHAT_ID, b"png-bytes", long_text) is True

    assert [call["url"].rsplit("/", 1)[-1] for call in calls] == ["sendPhoto", "sendMessage"]
    assert len(calls[0]["data"]["caption"]) <= notify.CAPTION_LIMIT
    assert calls[1]["data"]["text"] == long_text


def test_photo_failure_still_delivers_the_answer_as_text(monkeypatch):
    """sendPhoto and sendMessage fail independently (an oversized image, say)."""
    rejected = FakeResponse(
        {"ok": False, "error_code": 400, "description": "PHOTO_INVALID_DIMENSIONS"}
    )
    calls = install_fake_requests(monkeypatch, [rejected, FakeResponse({"ok": True})])
    assert notify.send(TOKEN, CHAT_ID, b"png", "the answer") is False
    assert [call["url"].rsplit("/", 1)[-1] for call in calls] == ["sendPhoto", "sendMessage"]
    assert calls[1]["data"]["text"] == "the answer"


def test_non_object_json_does_not_raise(monkeypatch):
    """A captive portal can return valid JSON that is not an object."""
    install_fake_requests(monkeypatch, [FakeResponse([]), FakeResponse([])])
    assert notify.send(TOKEN, CHAT_ID, b"png", "text") is False


def test_chat_id_is_masked_in_errors(monkeypatch, capsys):
    not_found = FakeResponse(
        {"ok": False, "error_code": 400, "description": f"chat {CHAT_ID} not found"}
    )
    install_fake_requests(monkeypatch, [not_found, not_found])
    notify.send(TOKEN, CHAT_ID, b"png", "text")
    assert CHAT_ID not in capsys.readouterr().out


def test_percent_encoded_token_is_scrubbed(monkeypatch, capsys):
    """requests percent-encodes the URL, which a literal search would miss."""
    from urllib.parse import quote

    token = "1234567890:AAH fake token with spaces"
    install_fake_requests(
        monkeypatch,
        [RuntimeError(f"Max retries exceeded with url: /bot{quote(token, safe='')}/sendPhoto")],
    )
    notify.send(token, CHAT_ID, b"png", "text")
    captured = capsys.readouterr().out + capsys.readouterr().err
    assert quote(token, safe="") not in captured
    assert token not in captured


def test_network_error_does_not_raise(monkeypatch):
    install_fake_requests(monkeypatch, [RuntimeError("connection reset")])
    assert notify.send(TOKEN, CHAT_ID, b"png", "text") is False


def test_bot_token_never_appears_in_output(monkeypatch, capsys):
    """requests embeds the URL (which contains the token) in its exceptions."""
    install_fake_requests(
        monkeypatch,
        [RuntimeError(f"HTTPSConnectionPool https://api.telegram.org/bot{TOKEN}/sendPhoto")],
    )
    notify.send(TOKEN, CHAT_ID, b"png", "text")
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_api_error_description_is_surfaced(monkeypatch, capsys):
    install_fake_requests(
        monkeypatch,
        [FakeResponse({"ok": False, "error_code": 403, "description": "bot was blocked"})],
    )
    notify.send(TOKEN, CHAT_ID, b"png", "text")
    assert "bot was blocked" in capsys.readouterr().out


def test_unparseable_response_is_handled(monkeypatch):
    install_fake_requests(monkeypatch, [FakeResponse(None, status_code=502)])
    assert notify.send(TOKEN, CHAT_ID, b"png", "text") is False


def test_verify_requires_both_fields():
    ok, message = notify.verify_credentials("", CHAT_ID)
    assert ok is False and "token" in message.lower()
    ok, message = notify.verify_credentials(TOKEN, "")
    assert ok is False and "chat id" in message.lower()


def test_verify_calls_get_me_then_send_message(monkeypatch):
    calls = install_fake_requests(
        monkeypatch, [FakeResponse({"ok": True}), FakeResponse({"ok": True})]
    )
    ok, _ = notify.verify_credentials(TOKEN, CHAT_ID)
    assert ok is True
    assert [call["url"].rsplit("/", 1)[-1] for call in calls] == ["getMe", "sendMessage"]
