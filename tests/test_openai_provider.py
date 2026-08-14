"""OpenAI provider — Chat Completions streaming and error translation."""

from __future__ import annotations

from typing import Any

import openai

from screenrecon import vision
from screenrecon.providers import openai as openai_provider

API_KEY = "sk-openai-fake-key-for-tests"


def _cfg(model: str = "gpt-4o", key: str = API_KEY, **extra: Any) -> dict:
    return {"model": model, "api_key": key, **extra}


# --------------------------------------------------------------------------- #
# Turn -> messages
# --------------------------------------------------------------------------- #


def test_user_turn_becomes_parts_list_with_image_first():
    turns = [vision.user_turn(b"jpeg", "read this")]
    messages = openai_provider._messages_from_turns(turns)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    parts = messages[0]["content"]
    assert [p["type"] for p in parts] == ["image_url", "text"]
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[1]["text"] == "read this"


def test_assistant_turn_collapses_to_string_content():
    turns = [vision.assistant_turn("done")]
    assert openai_provider._messages_from_turns(turns) == [
        {"role": "assistant", "content": "done"}
    ]


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


class FakeDelta:
    def __init__(self, content: str | None = None):
        self.content = content


class FakeChoice:
    def __init__(self, content: str | None = None):
        self.delta = FakeDelta(content)


class FakeEvent:
    def __init__(self, content: str | None = None):
        self.choices = [FakeChoice(content)] if content is not None else []


class FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __iter__(self):
        return iter(self._events)


class FakeChatCompletions:
    def __init__(self, result):
        self._result = result
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result):
        self.chat = type("Chat", (), {"completions": FakeChatCompletions(result)})()
        self.models = self  # for verify_key

    def retrieve(self, model):
        return {"id": model}  # verify_key ignores return content


def install(monkeypatch, client):
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
    return client


def test_ask_streaming_delivers_deltas_and_returns_joined_text(monkeypatch):
    events = [FakeEvent("Hel"), FakeEvent("lo"), FakeEvent(""), FakeEvent(None)]
    client = install(monkeypatch, FakeClient(FakeStream(events)))
    seen: list[str] = []
    reply = vision.ask_streaming(_cfg(), [vision.user_turn(b"jpg", "hi")], seen.append)
    assert reply.ok is True
    assert reply.text == "Hello"
    assert seen == ["Hel", "lo"]
    assert client.chat.completions.calls[0]["stream"] is True


def test_ask_streaming_translates_auth_errors(monkeypatch):
    from tests.test_vision import status_error  # reuse the httpx-based builder

    install(monkeypatch, FakeClient(status_error(openai.AuthenticationError, 401)))
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    assert "--key" in reply.text


def test_ask_streaming_reports_empty_answers(monkeypatch):
    """A stream that yields no text at all should be a failure, not silent success."""
    install(monkeypatch, FakeClient(FakeStream([FakeEvent(None), FakeEvent("")])))
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    assert "empty" in reply.text.lower()
