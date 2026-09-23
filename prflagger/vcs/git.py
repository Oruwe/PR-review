"""Small, read-only questions asked of git."""

from __future__ import annotations

import subprocess

from prflagger.vcs.credentials import run_git

__all__ = ["remote_head"]


def remote_head(source: str, branch: str, *, timeout_s: float = 60.0) -> str | None:
    """The commit a remote branch points at, without cloning or fetching.

    `git ls-remote` works the same against a GitHub URL, a self-hosted server
    and a local path, which is why branch movement is watched this way rather
    than through any one host's API.
    """
    try:
        completed = run_git(
            ["git", "ls-remote", "--heads", source, f"refs/heads/{branch}"],
            remote=source, timeout_s=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    first = completed.stdout.strip().splitlines()
    if not first:
        return None
    sha = first[0].split()[0]
    return sha if len(sha) >= 40 else None
