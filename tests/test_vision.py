"""AI error translation and request shaping (design doc 8.1)."""

from __future__ import annotations

import anthropic
import httpx
import pytest

from screenrecon import vision

API_KEY = "sk-ant-api03-fake-key-for-tests"


def status_error(cls, status_code: int, message: str = "boom"):
    """Build a real SDK exception; str(exc) is the message we pass in."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return cls(message, response=response, body=None)


# --------------------------------------------------------------------------- #
# Error translation (design doc 5.4)
# --------------------------------------------------------------------------- #


def test_authentication_error_points_at_configure():
    message = vision.translate_error(status_error(anthropic.AuthenticationError, 401))
    assert "invalid or revoked" in message
    assert "--configure" in message


def test_rate_limit_error_asks_to_retry():
    message = vision.translate_error(status_error(anthropic.RateLimitError, 429))
    assert "Too many requests" in message


def test_not_found_error_points_at_the_model_field():
    message = vision.translate_error(status_error(anthropic.NotFoundError, 404))
    assert "model" in message.lower()


def test_generic_status_error_includes_the_code():
    message = vision.translate_error(status_error(anthropic.InternalServerError, 529))
    assert "529" in message


def test_connection_error_mentions_the_network():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    message = vision.translate_error(anthropic.APIConnectionError(request=request))
    assert "Network connection failed" in message


def test_timeout_is_distinguished_from_a_plain_connection_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    message = vision.translate_error(anthropic.APITimeoutError(request=request))
    assert "timed out" in message


def test_unknown_error_is_scrubbed_of_credentials():
    message = vision.translate_error(RuntimeError(f"failed with key {API_KEY}"), [API_KEY])
    assert API_KEY not in message
    assert "RuntimeError" in message


# --------------------------------------------------------------------------- #
# Request shaping
# --------------------------------------------------------------------------- #


def test_image_block_is_base64_png():
    block = vision.image_block(b"\x89PNG fake bytes")
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
    assert isinstance(block["source"]["data"], str)


def test_user_turn_puts_the_image_before_the_text():
    turn = vision.user_turn(b"png", "what is this?")
    assert turn["role"] == "user"
    assert [block["type"] for block in turn["content"]] == ["image", "text"]


def test_user_turn_without_an_image_is_text_only():
    turn = vision.user_turn(None, "follow-up")
    assert [block["type"] for block in turn["content"]] == ["text"]


# --------------------------------------------------------------------------- #
# ask(): never raises, extracts every text block
# --------------------------------------------------------------------------- #


class FakeBlock:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class FakeMessage:
    def __init__(self, blocks, stop_reason="end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason


class FakeClient:
    """Stands in for anthropic.Anthropic; records every request it receives."""

    def __init__(self, results) -> None:
        self.results = list(results)
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def install_fake_client(monkeypatch, client):
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: client)
    return client


def test_ask_joins_all_text_blocks(monkeypatch):
    client = install_fake_client(
        monkeypatch,
        FakeClient([FakeMessage([FakeBlock("text", "first"), FakeBlock("text", "second")])]),
    )
    reply = vision.ask(API_KEY, "claude-opus-5", [vision.user_turn(b"png", "hi")])
    assert reply.ok is True
    assert reply.text == "first\nsecond"
    assert client.requests[0]["model"] == "claude-opus-5"
    assert client.requests[0]["max_tokens"] == vision.MAX_TOKENS


def test_ask_ignores_non_text_blocks(monkeypatch):
    install_fake_client(
        monkeypatch,
        FakeClient([FakeMessage([FakeBlock("thinking"), FakeBlock("text", "answer")])]),
    )
    assert vision.ask(API_KEY, "claude-opus-5", []).text == "answer"


def test_ask_translates_api_errors_instead_of_raising(monkeypatch):
    install_fake_client(
        monkeypatch, FakeClient([status_error(anthropic.AuthenticationError, 401)])
    )
    reply = vision.ask(API_KEY, "claude-opus-5", [])
    assert reply.ok is False
    assert "--configure" in reply.text


def test_ask_reports_truncated_answers(monkeypatch):
    install_fake_client(monkeypatch, FakeClient([FakeMessage([], stop_reason="max_tokens")]))
    reply = vision.ask(API_KEY, "claude-opus-5", [])
    assert reply.ok is False
    assert "output limit" in reply.text


def test_ask_reports_refusals(monkeypatch):
    install_fake_client(monkeypatch, FakeClient([FakeMessage([], stop_reason="refusal")]))
    reply = vision.ask(API_KEY, "claude-opus-5", [])
    assert reply.ok is False
    assert "declined" in reply.text


def test_effort_is_dropped_for_models_that_reject_it(monkeypatch):
    """Models without effort support must not break the tool (one retry, then cached)."""
    monkeypatch.setattr(vision, "_effort_unsupported", set())
    bad_request = status_error(
        anthropic.BadRequestError, 400, "output_config.effort is not supported by this model"
    )
    client = install_fake_client(
        monkeypatch, FakeClient([bad_request, FakeMessage([FakeBlock("text", "ok")])])
    )

    reply = vision.ask(API_KEY, "claude-haiku-4-5", [])
    assert reply.ok is True
    assert "output_config" in client.requests[0]
    assert "output_config" not in client.requests[1]
    assert "claude-haiku-4-5" in vision._effort_unsupported


def test_sdk_too_old_for_output_config_falls_back(monkeypatch):
    """An SDK predating output_config raises TypeError; retry without it rather than fail."""
    monkeypatch.setattr(vision, "_effort_unsupported", set())
    client = install_fake_client(
        monkeypatch,
        FakeClient(
            [
                TypeError("create() got an unexpected keyword argument 'output_config'"),
                FakeMessage([FakeBlock("text", "ok")]),
            ]
        ),
    )

    reply = vision.ask(API_KEY, "claude-opus-5", [])
    assert reply.ok is True
    assert "output_config" not in client.requests[1]


def test_unrelated_type_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(vision, "_effort_unsupported", set())
    client = install_fake_client(monkeypatch, FakeClient([TypeError("bad message shape")]))

    reply = vision.ask(API_KEY, "claude-opus-5", [])
    assert reply.ok is False
    assert len(client.requests) == 1


def test_other_bad_requests_are_not_retried(monkeypatch):
    monkeypatch.setattr(vision, "_effort_unsupported", set())
    error = status_error(anthropic.BadRequestError, 400, "messages: at least one is required")
    client = install_fake_client(monkeypatch, FakeClient([error]))

    reply = vision.ask(API_KEY, "claude-opus-5", [])
    assert reply.ok is False
    assert len(client.requests) == 1


def test_verify_key_rejects_an_empty_key():
    ok, message = vision.verify_key("", "claude-opus-5")
    assert ok is False
    assert "No API key" in message


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-haiku-4-5"])
def test_ask_image_builds_a_single_user_turn(monkeypatch, model):
    client = install_fake_client(
        monkeypatch, FakeClient([FakeMessage([FakeBlock("text", "done")])])
    )
    vision.ask_image(API_KEY, model, b"png", "read this")
    messages = client.requests[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
