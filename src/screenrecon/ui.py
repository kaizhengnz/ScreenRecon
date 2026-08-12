"""Terminal output helpers: consistent prefixes and credential masking.

Every credential is masked before it reaches the terminal or a log (NFR-3).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

MASK_KEEP = 8
"""Number of leading characters kept when masking (NFR-3: show first 8 only)."""

MIN_SCRUB_LEN = 4
"""Shortest secret worth replacing. Below this, a secret is mostly punctuation or
single digits, and replacing every occurrence would mangle the message without
protecting anything meaningful."""


def mask(secret: str | None) -> str:
    """Render a credential as its first 8 characters plus a length hint.

    Deliberately ASCII-only: masked credentials are printed on every platform,
    including legacy Windows code pages that cannot encode a typographic ellipsis.
    """
    if not secret:
        return "(not set)"
    text = str(secret)
    if len(text) <= MASK_KEEP:
        return text[0] + "*" * (len(text) - 1) if len(text) > 1 else "*"
    return f"{text[:MASK_KEEP]}... ({len(text)} chars)"


def scrub(text: str, secrets: Iterable[str | None]) -> str:
    """Strip credential plaintext out of arbitrary text before printing it.

    Third-party exceptions routinely embed the request URL, and the Telegram URL
    contains the bot token — so anything originating outside this package must
    pass through here first.

    This is a literal replacement, so it only defends against a secret that
    survives verbatim. Callers that know a secret may be re-encoded on the wire
    (percent-encoding in a URL, for example) must pass those variants in too.
    """
    result = str(text)
    for secret in secrets:
        if secret and len(str(secret)) >= MIN_SCRUB_LEN:
            result = result.replace(str(secret), mask(secret))
    return result


def info(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[warn] {message}", flush=True)


def error(message: str) -> None:
    print(f"[error] {message}", file=sys.stderr, flush=True)


def rule(title: str = "") -> None:
    """Print a separator line, optionally with a title."""
    if title:
        print(f"\n---- {title} " + "-" * max(0, 40 - len(title)), flush=True)
    else:
        print("-" * 48, flush=True)
