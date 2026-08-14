"""AI dispatcher (design doc 5.4).

This module owns the *provider-agnostic* API the rest of the codebase uses to
talk to a vision model:

- ``Turn`` — one message in the conversation (user / assistant, optional image).
- ``Reply`` — the outcome of a call (ok + text, or a readable error).
- ``ask_streaming(cfg, turns, on_delta)`` — stream a response; provider chosen
  from ``cfg["provider"]`` (explicit) or the model name prefix.
- ``verify_key(cfg)`` — cheap probe used by the wizard.

The provider layer lives under :mod:`screenrecon.providers`. Each provider
translates ``Turn`` sequences into its SDK's message format, streams the
response, and translates SDK exceptions into a readable ``Reply.text``. Adding
a provider is a new file under ``providers/`` and one line in ``_PROVIDERS``.

The ``ask*`` and ``verify_key`` functions never raise: every failure is
translated into a readable ``Reply`` (or a ``(False, message)`` tuple), so the
watch loop keeps running (NFR-2).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

MAX_TOKENS = 4096
"""Covers thinking plus the visible answer. Current Opus models run adaptive
thinking by default and this cap applies to both, so a tight budget can be
spent entirely on thinking and return no text at all. Applies to every
provider — 4096 is enough for OCR / short-description output on all of them."""

EFFORT = "low"
"""Reading a screenshot is a light task; low effort keeps latency and cost
down where the provider exposes such a knob. Providers that do not accept the
parameter (or a specific model that rejects it) fall back transparently."""


@dataclass(frozen=True)
class Turn:
    """One conversation turn, provider-agnostic.

    ``role`` is ``"user"`` or ``"assistant"``. ``image`` is the JPEG bytes of
    a screenshot for user turns that carry one, or ``None`` for text-only
    turns. Providers translate this into their own SDK's message format.
    """

    role: str
    text: str
    image: bytes | None = None


@dataclass(frozen=True)
class Reply:
    """One AI call. When ``ok`` is False, ``text`` is a readable error message."""

    ok: bool
    text: str


class Provider(Protocol):
    """A vision-capable AI provider — Anthropic, OpenAI, Google, or an
    OpenAI-compatible endpoint (DeepSeek / Kimi / Doubao). See
    :mod:`screenrecon.providers` for implementations."""

    name: str
    """Stable identifier used in ``cfg["provider"]`` and in the registry."""

    display_name: str
    """Human-readable label the wizard shows to the user."""

    def ask_streaming(
        self,
        cfg: Mapping[str, Any],
        turns: Sequence[Turn],
        on_delta: Callable[[str], None],
    ) -> Reply:
        """Stream a response, calling ``on_delta`` with each text chunk."""
        ...

    def verify_key(self, cfg: Mapping[str, Any]) -> tuple[bool, str]:
        """Cheap probe used by ``--configure`` to validate the key + model."""
        ...


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #


_PROVIDERS: dict[str, Provider] = {}
"""Populated at import time by ``providers/__init__.py``. Keys match
``Provider.name`` and the accepted values of ``cfg["provider"]``."""

_PREFIX_MAP: list[tuple[str, str]] = [
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini-", "google"),
]
"""Model-name-prefix → provider name. Falls through to ``anthropic`` for
unknown prefixes so any existing configs written before this refactor keep
working. The OpenAI-compatible provider is *never* inferred from the model
name — it needs an explicit ``cfg["provider"] = "openai_compatible"`` (with a
``base_url``) because Chinese providers use model IDs like ``deepseek-vl2``
that clash with nothing but also match nothing."""


def register(provider: Provider) -> None:
    """Install a provider in the registry. Called from ``providers/__init__.py``."""
    _PROVIDERS[provider.name] = provider


def known_providers() -> list[Provider]:
    """Return every registered provider (order stable across runs). Used by
    the wizard to build the two-step 'pick provider, then pick model' UI."""
    return [_PROVIDERS[name] for name in sorted(_PROVIDERS)]


def get_provider(cfg: Mapping[str, Any]) -> Provider:
    """Resolve the provider for ``cfg``.

    Priority: explicit ``cfg["provider"]`` wins; otherwise infer from the
    model-name prefix; otherwise default to ``anthropic`` (pre-refactor
    behaviour). Unknown explicit values raise ``KeyError`` — the wizard is
    the only writer of that field, so an unknown value means a hand-edited
    config and the user deserves a clear failure.
    """
    explicit = str(cfg.get("provider") or "").strip()
    if explicit:
        if explicit not in _PROVIDERS:
            raise KeyError(
                f"Unknown provider {explicit!r} in config. "
                f"Known providers: {', '.join(sorted(_PROVIDERS))}."
            )
        return _PROVIDERS[explicit]

    model = str(cfg.get("model", ""))
    for prefix, name in _PREFIX_MAP:
        if model.startswith(prefix) and name in _PROVIDERS:
            return _PROVIDERS[name]
    return _PROVIDERS["anthropic"]


# --------------------------------------------------------------------------- #
# Top-level API — dispatchers + Turn constructors
# --------------------------------------------------------------------------- #


def user_turn(image: bytes | None, text: str) -> Turn:
    """Build a user turn with an optional image."""
    return Turn(role="user", text=text, image=image)


def assistant_turn(text: str) -> Turn:
    """Build an assistant turn (text only — models never emit images in this tool)."""
    return Turn(role="assistant", text=text)


def ask_streaming(
    cfg: Mapping[str, Any],
    turns: Sequence[Turn],
    on_delta: Callable[[str], None],
) -> Reply:
    """Stream a response from whichever provider ``cfg`` selects.

    ``on_delta`` runs on the current thread as each text chunk arrives; keep
    it fast (``print(..., flush=True)`` is the intended use). On failure the
    callback is not invoked — the error surfaces as ``Reply(ok=False,
    text=readable_message)`` so the caller can print the message itself.
    Zero-delta success (refusal / max_tokens) is also translated into a
    non-ok Reply.
    """
    return get_provider(cfg).ask_streaming(cfg, turns, on_delta)


def verify_key(cfg: Mapping[str, Any]) -> tuple[bool, str]:
    """Cheap probe used by ``--configure`` and ``--key``.

    Returns ``(True, message)`` on success or ``(False, message)`` on
    failure. Never raises — a broken network or an unknown model is a
    verification failure, not a crash.
    """
    return get_provider(cfg).verify_key(cfg)


# --------------------------------------------------------------------------- #
# Provider registration
# --------------------------------------------------------------------------- #
# Imported here (at the bottom, after the types the providers depend on) so
# that ``import screenrecon.vision`` transitively loads and registers every
# provider. Callers never need to import the providers module directly.
from . import providers  # noqa: E402, F401
