"""Identifier generation.

Run and job ids are sortable by creation time and readable in a URL, because
they end up in both. Observation ids are content-addressed so the same probe
finding the same fact twice produces the same id — which is what lets a re-run
be compared against its predecessor.
"""

from __future__ import annotations

import hashlib
import os
import time

__all__ = ["job_id", "observation_id", "run_id", "slug_key"]

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32: no i, l, o, u


def _b32(value: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def run_id(*, now: float | None = None) -> str:
    """A lexicographically sortable run id: `r<time><random>`.

    Sorting by id sorts by creation time, so "the latest run" is a string
    comparison rather than a join against `created_at`.
    """
    stamp = int((now if now is not None else time.time()) * 1000)
    return f"r{_b32(stamp, 10)}{_b32(int.from_bytes(os.urandom(5), 'big'), 8)}"


def job_id(run: str, stage: str, ordinal: int = 0) -> str:
    """Deterministic per (run, stage, ordinal) so a retry reuses its log file."""
    return f"{run}-{stage}-{ordinal}"


def observation_id(run: str, kind: str, symbol: str, evidence: str) -> str:
    """Content-addressed: the same fact about the same symbol gets the same id."""
    digest = hashlib.sha256(
        "\0".join((kind, symbol, evidence)).encode("utf-8", "replace")
    ).hexdigest()
    return f"o{digest[:16]}" if not run else f"{run}-o{digest[:16]}"


def slug_key(slug: str) -> str:
    """A filesystem-safe form of `owner/name`."""
    return slug.replace("/", "__")
