"""Changed line ranges, straight from git."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

__all__ = ["changed_ranges"]

# @@ -old,oldcount +new,newcount @@
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_ranges(repo: Path, base: str, head: str) -> dict[str, list[tuple[int, int]]]:
    """repo-relative file path -> changed line ranges in the HEAD revision."""
    completed = _git(
        ["diff", "--unified=0", "--no-color", "--find-renames", base, head], repo
    )
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None

    for line in completed.stdout.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            # /dev/null means the file is gone at head; it has no head-side lines.
            current = None if target == "/dev/null" else _strip_prefix(target)
            continue
        if not line.startswith("@@") or current is None:
            continue
        match = _HUNK.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        if count == 0:
            continue  # a pure deletion adds no lines to the head revision
        ranges.setdefault(current, []).append((start, start + count - 1))

    return {path: _merge(spans) for path, spans in ranges.items() if spans}


def _strip_prefix(target: str) -> str:
    """git writes `b/path`; quoted paths come wrapped in double quotes."""
    if target.startswith('"') and target.endswith('"'):
        target = target[1:-1]
    return target[2:] if target.startswith(("a/", "b/")) else target


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _git(argv: Sequence[str], repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *argv],
        capture_output=True,
        text=True,
        check=False,
    )
