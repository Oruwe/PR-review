"""The fallback pack: run whatever the repo's own CI runs.

This is what makes "any repo" true rather than aspirational. When no ecosystem
pack claims a repo, we read its GitHub Actions workflow or its Makefile and run
the commands it already declares for itself. The analysis is shallower — no
symbol graph, no coverage — but the repo still gets a real sandboxed run and
honest lint/test observations, and the report says plainly which depth applied.
"""

from __future__ import annotations

import re
from pathlib import Path

from prflagger.lang.base import TestCommand, Toolchain

__all__ = ["GENERIC", "discover_commands"]

GENERIC = Toolchain(
    id="generic",
    display="Generic (repo's own CI)",
    markers=(),  # never auto-detected by marker; chosen when nothing else claims the repo
    base_image="debian:bookworm-slim",
    test=TestCommand(argv=("sh", "-c", "make test")),
    source_globs=("**/*",),
    lockfiles=("Makefile", "justfile", "Taskfile.yml"),
)

_RUN_STEP = re.compile(r"^\s*-?\s*run:\s*(?:\|)?\s*(.*)$")
_MAKE_TARGET = re.compile(r"^(test|check|lint|ci)\s*:")

#: Commands we will never lift out of a repo's CI config, however it spells them.
#: A workflow is untrusted input: it belongs to whoever opened the pull request.
_REFUSED = re.compile(
    r"\b(curl|wget|ssh|scp|rsync|nc|docker|kubectl|aws|gcloud|az|npm\s+publish"
    r"|pip\s+config|git\s+push|sudo|chmod\s+\+s|/dev/tcp)\b"
)


def discover_commands(repo_path: Path) -> tuple[tuple[str, ...], ...]:
    """Test-ish commands the repo declares for itself, in preference order.

    Read as *data*, never executed here — the caller runs them inside the
    sandbox, with no network and no capabilities, exactly like any other pack.
    Anything matching `_REFUSED` is dropped: a workflow is contributed content,
    and a PR that adds `curl | sh` to its own CI must not get that run for it
    even inside the sandbox.
    """
    found: list[tuple[str, ...]] = []

    for workflow in sorted((repo_path / ".github" / "workflows").glob("*.y*ml"))[:5]:
        try:
            text = workflow.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _RUN_STEP.match(line)
            if not match:
                continue
            command = match.group(1).strip().strip("\"'")
            if not command or _REFUSED.search(command):
                continue
            if any(word in command for word in ("test", "pytest", "check", "lint")):
                found.append(("sh", "-c", command))

    makefile = repo_path / "Makefile"
    if makefile.is_file():
        try:
            for line in makefile.read_text(encoding="utf-8", errors="replace").splitlines():
                target = _MAKE_TARGET.match(line)
                if target:
                    found.append(("make", target.group(1)))
        except OSError:
            pass

    # Preserve order, drop duplicates.
    seen: set[tuple[str, ...]] = set()
    unique = [c for c in found if not (c in seen or seen.add(c))]  # type: ignore[func-returns-value]
    return tuple(unique[:6])
