"""Checking every reference a model makes, before it is believed.

Four kinds of reference, each resolved against something real:

* ``norm``  — a norm id this repository actually has.
* ``code``  — ``path:line`` or ``path:start-end`` at the head commit, or
  ``base:path:line`` at the base; the lines must exist, and a quote, if given,
  must appear in them. A charter claim's own source (``README.md:12``) also
  resolves, against the text the charter recorded for it.
* ``test``  — a test id the run's own suite produced, at either commit.
* ``diff``  — a file this pull request changes.

A reference that does not resolve is removed. A misquote is treated exactly
like a missing line: quoting text that is not there is how an invented claim
looks, so it is not given the benefit of the doubt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from prflagger.core.models import Citation

__all__ = ["Evidence", "resolve"]

_CODE = re.compile(r"^(?:(base|head):)?(.+?):(\d+)(?:-(\d+))?$")
_MAX_SPAN = 120  # lines; a citation of half a file is not a citation


@dataclass(frozen=True)
class Evidence:
    """Everything a citation made during one run may point at."""

    head_tree: Path
    base_tree: Path
    norm_ids: frozenset[str] = frozenset()
    test_ids: frozenset[str] = frozenset()
    changed_paths: frozenset[str] = frozenset()
    charter_sources: dict[str, str] = field(default_factory=dict)


def resolve(citation: Citation, evidence: Evidence) -> Citation | None:
    """The citation, with its quote checked or filled in — or None."""
    ref = citation.ref.strip()
    if citation.type == "norm":
        return Citation("norm", ref, citation.quote) if ref in evidence.norm_ids else None
    if citation.type == "test":
        return Citation("test", ref, citation.quote) if ref in evidence.test_ids else None
    if citation.type == "diff":
        path = ref.split(":", 1)[0].removeprefix("head:")
        return Citation("diff", ref, citation.quote) if path in evidence.changed_paths else None
    if citation.type == "code":
        return _resolve_code(ref, citation.quote, evidence)
    return None


def _resolve_code(ref: str, quote: str, evidence: Evidence) -> Citation | None:
    if ref in evidence.charter_sources:
        claim = evidence.charter_sources[ref]
        if quote and not _contains(claim, quote):
            return None
        return Citation("code", ref, quote or claim[:200])

    match = _CODE.match(ref)
    if match is None:
        return None
    side, raw_path, first, last = match.groups()
    start = int(first)
    end = int(last) if last else start
    if start < 1 or end < start or end - start > _MAX_SPAN:
        return None
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts:
        return None
    tree = evidence.base_tree if side == "base" else evidence.head_tree
    target = (tree / path).resolve()
    if not target.is_relative_to(tree.resolve()) or not target.is_file():
        return None
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if end > len(lines):
        return None
    excerpt = "\n".join(lines[start - 1:end])
    if quote and not _contains(excerpt, quote):
        return None
    return Citation("code", ref, quote or excerpt.strip()[:200])


def _contains(haystack: str, needle: str) -> bool:
    """Whitespace-insensitive containment, so reflowed quotes still match."""
    squash = " ".join
    return squash(needle.split()) in squash(haystack.split())
