"""Dispatcher + Anthropic provider tests (design doc 5.4).

Split responsibilities:

- Dispatcher / Turn / provider registry tests use only ``vision``.
- Anthropic-specific tests (error translation, message shape, streaming
  fallback) call into ``providers.anthropic`` directly. FakeClient patches
  ``anthropic.Anthropic`` so the provider hits it under the covers.

When adding another provider, add a sibling ``test_providers_<name>.py``.
This file stays anthropic-focused because the dispatch layer is thin.
"""

from __future__ import annotations

import anthropic
import httpx
import pytest

from screenrecon import vision
from screenrecon.providers import anthropic as anthropic_provider

API_KEY = "sk-ant-api03-fake-key-for-tests"


def status_error(cls, status_code: int, message: str = "boom"):
    """Build a real SDK exception; ``str(exc)`` is the message we pass in."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return cls(message, response=response, body=None)


def _cfg(model: str = "claude-opus-5", key: str = API_KEY) -> dict:
    return {"model": model, "anthropic_api_key": key}


# --------------------------------------------------------------------------- #
# Anthropic error translation
# --------------------------------------------------------------------------- #


def test_authentication_error_points_at_configure():
    message = anthropic_provider.translate_error(status_error(anthropic.AuthenticationError, 401))
    assert "invalid or revoked" in message
    assert "--configure" in message


def test_rate_limit_error_asks_to_retry():
    message = anthropic_provider.translate_error(status_error(anthropic.RateLimitError, 429))
    assert "Too many requests" in message


def test_not_found_error_points_at_the_model_field():
    message = anthropic_provider.translate_error(status_error(anthropic.NotFoundError, 404))
    assert "model" in message.lower()


def test_generic_status_error_includes_the_code():
    message = anthropic_provider.translate_error(status_error(anthropic.InternalServerError, 529))
    assert "529" in message


def test_connection_error_mentions_the_network():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    message = anthropic_provider.translate_error(anthropic.APIConnectionError(request=request))
    assert "Network connection failed" in message


def test_timeout_is_distinguished_from_a_plain_connection_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    message = anthropic_provider.translate_error(anthropic.APITimeoutError(request=request))
    assert "timed out" in message


def test_unknown_error_is_scrubbed_of_credentials():
    message = anthropic_provider.translate_error(
        RuntimeError(f"failed with key {API_KEY}"), [API_KEY]
    )
    assert API_KEY not in message
    assert "RuntimeError" in message


# --------------------------------------------------------------------------- #
# Turn constructors
# --------------------------------------------------------------------------- #


def test_user_turn_carries_the_image_and_text():
    turn = vision.user_turn(b"jpeg", "what is this?")
    assert isinstance(turn, vision.Turn)
    assert turn.role == "user"
    assert turn.text == "what is this?"
    assert turn.image == b"jpeg"


def test_user_turn_without_an_image_is_text_only():
    turn = vision.user_turn(None, "follow-up")
    assert turn.image is None
    assert turn.role == "user"


def test_assistant_turn_is_text_only():
    turn = vision.assistant_turn("the answer")
    assert turn.role == "assistant"
    assert turn.image is None
    assert turn.text == "the answer"


# --------------------------------------------------------------------------- #
# Anthropic message shape
# --------------------------------------------------------------------------- #


def test_image_block_is_base64_jpeg():
    block = anthropic_provider.image_block(b"\xff\xd8\xff fake jpeg bytes")
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    assert isinstance(block["source"]["data"], str)


def test_messages_from_turns_puts_the_image_before_the_text():
    turns = [vision.user_turn(b"jpeg", "what is this?")]
    messages = anthropic_provider._messages_from_turns(turns)
    assert messages[0]["role"] == "user"
    assert [b["type"] for b in messages[0]["content"]] == ["image", "text"]


def test_messages_from_turns_text_only_when_no_image():
    turns = [vision.user_turn(None, "follow-up")]
    messages = anthropic_provider._messages_from_turns(turns)
    assert [b["type"] for b in messages[0]["content"]] == ["text"]


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-haiku-4-5"],
)
def test_get_provider_picks_anthropic_for_claude_models(model):
    assert vision.get_provider({"model": model}).name == "anthropic"


def test_get_provider_defaults_to_anthropic_for_unknown_prefix():
    """Backward-compat: hand-edited configs with weird model names still route."""
    assert vision.get_provider({"model": "brand-new-model-xyz"}).name == "anthropic"


def test_get_provider_explicit_value_wins_over_prefix_inference():
    provider = vision.get_provider({"provider": "anthropic", "model": "gpt-4o"})
    assert provider.name == "anthropic"


def test_get_provider_raises_on_unknown_explicit_value():
    with pytest.raises(KeyError, match="Unknown provider"):
        vision.get_provider({"provider": "not-a-provider", "model": "claude-opus-5"})


# --------------------------------------------------------------------------- #
# Streaming — Anthropic path
# --------------------------------------------------------------------------- #


class FakeBlock:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class FakeMessage:
    def __init__(self, blocks, stop_reason="end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason


class FakeStream:
    """Context-manager stand-in for ``client.messages.stream(...)``."""

    def __init__(self, chunks, final) -> None:
        self._chunks = list(chunks)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(self._chunks)

    def get_final_message(self):
        return self._final


class FakeClient:
    """Stands in for ``anthropic.Anthropic``; records every request it receives."""

    def __init__(self, results) -> None:
        self.results = list(results)
        self.requests: list[dict] = []
        self.messages = self

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def install_fake_client(monkeypatch, client):
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: client)
    return client


def test_ask_streaming_delivers_chunks_in_order_and_returns_the_full_text(monkeypatch):
    stream = FakeStream(
        ["Hel", "lo, ", "world"], FakeMessage([FakeBlock("text", "Hello, world")])
    )
    install_fake_client(monkeypatch, FakeClient([stream]))
    seen: list[str] = []
    reply = vision.ask_streaming(_cfg(), [], seen.append)
    assert seen == ["Hel", "lo, ", "world"]
    assert reply.ok is True
    assert reply.text == "Hello, world"


def test_ask_streaming_translates_transport_errors_instead_of_raising(monkeypatch):
    install_fake_client(
        monkeypatch, FakeClient([status_error(anthropic.AuthenticationError, 401)])
    )
    seen: list[str] = []
    reply = vision.ask_streaming(_cfg(), [], seen.append)
    assert reply.ok is False
    assert seen == []  # nothing streamed on failure
    assert "--configure" in reply.text


def test_ask_streaming_reports_refusals(monkeypatch):
    install_fake_client(
        monkeypatch,
        FakeClient([FakeStream([], FakeMessage([], stop_reason="refusal"))]),
    )
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    assert "declined" in reply.text


def test_ask_streaming_reports_max_tokens(monkeypatch):
    install_fake_client(
        monkeypatch,
        FakeClient([FakeStream([], FakeMessage([], stop_reason="max_tokens"))]),
    )
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    assert "output limit" in reply.text


def test_ask_streaming_recovers_text_from_final_message_when_stream_empty(monkeypatch):
    """Some SDK versions materialise text only in the final message. Fall back to it."""
    install_fake_client(
        monkeypatch,
        FakeClient([FakeStream([], FakeMessage([FakeBlock("text", "final")]))]),
    )
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is True
    assert reply.text == "final"


def test_ask_streaming_falls_back_when_effort_is_unsupported(monkeypatch):
    """Drop output_config on a 400 that names it, then stream a second time."""
    monkeypatch.setattr(anthropic_provider, "_effort_unsupported", set())
    bad_request = status_error(
        anthropic.BadRequestError,
        400,
        "output_config.effort is not supported by this model",
    )
    good = FakeStream(["ok"], FakeMessage([FakeBlock("text", "ok")]))
    client = install_fake_client(monkeypatch, FakeClient([bad_request, good]))

    reply = vision.ask_streaming(_cfg("claude-haiku-4-5"), [], lambda _: None)
    assert reply.ok is True
    assert "output_config" in client.requests[0]
    assert "output_config" not in client.requests[1]
    assert "claude-haiku-4-5" in anthropic_provider._effort_unsupported


def test_ask_streaming_falls_back_when_sdk_predates_output_config(monkeypatch):
    """An SDK older than the parameter raises TypeError; retry without it."""
    monkeypatch.setattr(anthropic_provider, "_effort_unsupported", set())
    client = install_fake_client(
        monkeypatch,
        FakeClient(
            [
                TypeError("stream() got an unexpected keyword argument 'output_config'"),
                FakeStream(["ok"], FakeMessage([FakeBlock("text", "ok")])),
            ]
        ),
    )

    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is True
    assert "output_config" not in client.requests[1]


def test_ask_streaming_does_not_retry_unrelated_type_errors(monkeypatch):
    monkeypatch.setattr(anthropic_provider, "_effort_unsupported", set())
    client = install_fake_client(
        monkeypatch, FakeClient([TypeError("bad message shape")])
    )
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    assert len(client.requests) == 1


def test_ask_streaming_does_not_retry_unrelated_bad_requests(monkeypatch):
    monkeypatch.setattr(anthropic_provider, "_effort_unsupported", set())
    error = status_error(
        anthropic.BadRequestError, 400, "messages: at least one is required"
    )
    client = install_fake_client(monkeypatch, FakeClient([error]))
    reply = vision.ask_streaming(_cfg(), [], lambda _: None)
    assert reply.ok is False
    assert len(client.requests) == 1


# --------------------------------------------------------------------------- #
# verify_key
# --------------------------------------------------------------------------- #


def test_verify_key_rejects_an_empty_key():
    ok, message = vision.verify_key(_cfg(key=""))
    assert ok is False
    assert "No API key" in message
