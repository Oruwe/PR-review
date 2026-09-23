"""What a test needs from the machine, and why it skips when that is missing.

PR Flagger runs this suite on itself in its own sandbox, which has no network,
no Docker and no git, and mounts the checkout read-only. A test that needs one of
those says so and skips there. Nothing is mocked to make it pass. Where the tool
exists (CI, a developer's machine) none of this applies, so a skip can never hide
a failure there.
"""

from __future__ import annotations

import shutil

import pytest

__all__ = ["MISSING_TOOL_REASONS", "missing_tool", "require_git"]

MISSING_TOOL_REASONS = {
    "git": "git is not installed here; these tests run real git, never a mock",
    "docker": "Docker is not installed here; these tests start real containers",
}


def require_git() -> None:
    """Skip the calling test when git is not installed."""
    if shutil.which("git") is None:
        pytest.skip(MISSING_TOOL_REASONS["git"])


def missing_tool(error: BaseException | None) -> str | None:
    """The tool whose absence caused `error`, if that is what caused it.

    Only an executable that is truly not installed counts: the `FileNotFoundError`
    that starting it raises, anywhere in the exception's chain, for a name that
    is not on PATH.
    """
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, FileNotFoundError):
            name = error.filename
            if name in MISSING_TOOL_REASONS and shutil.which(str(name)) is None:
                return str(name)
        error = error.__cause__ or error.__context__
    return None
