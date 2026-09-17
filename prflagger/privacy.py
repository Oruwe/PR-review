"""The PII boundary.

Diffs, commit messages and review comments carry real contributor emails and,
occasionally, pasted secrets. Every one of those strings passes through here
before it reaches an LLM prompt, the on-disk cache, or a log line — the same
"one door" discipline `llm.py` already applies to the network call itself.

Redaction is mechanical (regex), never an LLM: guessing at PII with a model
would make the boundary itself untrustworthy in the same way `analysis/`
never uses an LLM to find structure. False positives (an over-eager mask) are
the safe failure; false negatives are not.
"""

from __future__ import annotations

import re

__all__ = ["redact"]

# Order matters: secrets before emails, since a leaked key can itself contain
# an '@'-free pattern that would otherwise slip past the email rule.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AWS access key IDs (AKIA/ASIA + 16 alnum) and the long-form secret keys.
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY_ID]"),
    # A 40-char base64-shaped secret — but not a 40-char git SHA, which is
    # this system's own evidence trail (`how_we_know` cites them by the
    # thousand). A SHA is pure lowercase hex; a real secret almost never is.
    (
        re.compile(r"\b(?![0-9a-f]{40}\b)[A-Za-z0-9/+=]{40}\b"),
        "[REDACTED_SECRET]",
    ),
    # Bedrock bearer tokens: base64, long, no fixed length.
    (re.compile(r"\bAB[A-Za-z0-9+/=]{60,}\b"), "[REDACTED_BEARER_TOKEN]"),
    # Generic "key": "value" / "token=value" secrets in pasted config.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*"
            r"['\"]?[A-Za-z0-9/+=_-]{12,}['\"]?"
        ),
        r"\1=[REDACTED]",
    ),
    # Email addresses — the main PII surface in git commit trailers and
    # review comments (Co-authored-by, Reviewed-by, @mentions with emails).
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
]


def redact(text: str) -> str:
    """Mask emails and secret-shaped substrings. Idempotent and order-stable.

    Applied to every prompt and every embedding input before it is hashed,
    cached, or sent to a provider — nothing bypasses this by calling a
    provider directly, because nothing but `llm.py` is allowed to.
    """
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
