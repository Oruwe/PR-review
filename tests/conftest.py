"""Shared fixtures. Everything here uses the real target repo from config.toml."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def target() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "config.toml").read_text(encoding="utf-8"))
    return dict(data["target"])


@pytest.fixture(scope="session")
def head_sha(target: dict[str, str]) -> str:
    from prflagger.sandbox.runner import bare_clone

    bare = bare_clone(str(target["slug"]))
    completed = subprocess.run(
        ["git", f"--git-dir={bare}", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture(scope="session")
def head_worktree(target: dict[str, str], head_sha: str) -> Path:
    from prflagger.sandbox.runner import worktree_for

    return worktree_for(str(target["slug"]), head_sha).resolve()


@pytest.fixture(scope="session")
def image_key(head_worktree: Path) -> str:
    from prflagger.sandbox.runner import lockfile_image_key

    return lockfile_image_key(head_worktree)
