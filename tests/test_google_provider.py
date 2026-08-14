"""Google Gemini provider — content shaping, streaming, error translation."""

from __future__ import annotations

from typing import Any

import pytest

from screenrecon import vision
from screenrecon.providers import google as google_provider

API_KEY = "AIza-fake-google-key-for-tests"


def _cfg(model: str = "gemini-2.0-flash", key: str = API_KEY, **extra: Any) -> dict:
    return {"model": model, "api_key": key, **extra}


# --------------------------------------------------------------------------- #
# Turn -> contents
# --------------------------------------------------------------------------- #


def test_contents_alternate_user_and_model_roles():
    """Gemini expects assistant turns to use the 'model' role, not 'assistant'."""
    turns = [
        vision.user_turn(b"jpg", "first question"),
        vision.assistant_turn("first answer"),
        vision.user_turn(None, "follow-up"),
    ]
    contents = google_provider._contents_from_turns(turns)
    assert [c.role for c in contents] == ["user", "model", "user"]


def test_user_turn_puts_image_before_text():
    turns = [vision.user_turn(b"jpg", "read this")]
    parts = google_provider._contents_from_turns(turns)[0].parts
    # The first part carries the image (from_bytes -> inline_data), the
    # second part carries the text.
    assert getattr(parts[0], "inline_data", None) is not None
    assert getattr(parts[1], "text", None) == "read this"


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


class FakeChunk:
    def __init__(self, text: str | None):
        self.text = text


class FakeGenerator:
    def __init__(self, chunks_or_exc):
        self._items = chunks_or_exc

    def __iter__(self):
        if isinstance(self._items, Exception):
            raise self._items
        return iter(self._items)


class FakeModels:
    def __init__(self, stream_result=None, get_result=None):
        self._stream = stream_result
        self._get = get_result
        self.stream_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def generate_content_stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return FakeGenerator(self._stream)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        if isinstance(self._get, Exception):
            raise self._get
        return self._get or {"name": kwargs.get("model")}


class FakeClient:
    def __init__(self, stream_result=None, get_result=None):
        self.models = FakeModels(stream_result, get_result)


def install(monkeypatch, client):
    from google import genai

    monkeypatch.setattr(genai, "Client", lambda **kw: client)
    return client


def test_ask_streaming_forwards_deltas_and_joins_the_full_text(monkeypatch):
    client = install(
        monkeypatch,
        FakeClient(stream_result=[FakeChunk("Hel"), FakeChunk("lo"), FakeChunk(None)]),
    )
    seen: list[str] = []
    reply = vision.ask_streaming(_cfg(), [vision.user_turn(b"jpg", "hi")], seen.append)
    assert reply.ok is True
    assert reply.text == "Hello"
    assert seen == ["Hel", "lo"]
    assert client.models.stream_calls[0]["model"] == "gemini-2.0-flash"


def test_ask_streaming_reports_empty_answers(monkeypatch):
    install(monkeypatch, FakeClient(stream_result=[FakeChunk(None), FakeChunk("")]))
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    assert "empty" in reply.text.lower()


def test_ask_streaming_translates_transport_errors(monkeypatch):
    install(monkeypatch, FakeClient(stream_result=RuntimeError("unauthorized: bad api key")))
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    # translate_error catches the "api key" heuristic and points at --key
    assert "--key" in reply.text or "invalid" in reply.text.lower()


# --------------------------------------------------------------------------- #
# Error translation heuristics
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (RuntimeError("Unauthorized"), "invalid"),
        (RuntimeError("429 rate limit exceeded"), "Too many requests"),
        (RuntimeError("model not found"), "Model"),
        (RuntimeError("Request timed out after 30 s"), "timed out"),
        (RuntimeError("network is unreachable"), "Network"),
    ],
)
def test_translate_error_maps_common_shapes(exc, needle):
    assert needle in google_provider.translate_error(exc)


def test_translate_error_scrubs_credentials():
    message = google_provider.translate_error(RuntimeError(f"boom with {API_KEY}"), [API_KEY])
    assert API_KEY not in message
