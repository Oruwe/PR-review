"""Bare clones and worktrees: getting a commit onto disk to be mounted.

One bare clone per repo, one worktree per commit, never re-cloned. Worktrees are
shared between jobs on the same commit rather than duplicated — they are mounted
`:ro` and nothing writes to them after creation, so sharing is safe, and a
second checkout of a large repo is expensive. Creation itself is serialised, so
two concurrent jobs cannot race `git worktree add`.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import structlog

from prflagger.core.config import cache_root
from prflagger.core.errors import RepoUnavailable
from prflagger.gitsafety import assert_safe_revision
from prflagger.vcs.credentials import run_git

__all__ = ["bare_clone", "ensure_readable", "merge_base", "worktree_for"]

log = structlog.get_logger(__name__)

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _slug_dir(slug: str) -> str:
    return slug.replace("/", "__")


def bare_clone(slug: str, *, url: str | None = None) -> Path:
    """One bare clone per repo under `.cache/repos/`. Never re-cloned."""
    path = cache_root() / "repos" / f"{_slug_dir(slug)}.git"
    with _lock_for(f"clone:{slug}"):
        if path.is_dir():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        source = url or f"https://github.com/{slug}"
        completed = _git(
            ["clone", "--bare", "--filter=blob:none", source, str(path)],
            cwd=None,
            timeout_s=1800,
            remote=source,
        )
        if completed.returncode != 0:
            raise RepoUnavailable(f"could not clone {slug}: {completed.stderr.strip()[:400]}")
    log.info("repo.cloned", slug=slug)
    return path


def worktree_for(slug: str, commit: str, *, url: str | None = None) -> Path:
    """A checkout of `commit`, ready to mount. Fetches only if the commit is absent."""
    assert_safe_revision(commit)
    bare = bare_clone(slug, url=url)
    path = cache_root() / "worktrees" / _slug_dir(slug) / commit

    with _lock_for(f"worktree:{slug}:{commit}"):
        if path.is_dir():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_commit(bare, slug, commit, url)
        completed = _git(
            ["worktree", "add", "--detach", "--quiet", str(path), commit],
            cwd=bare,
            timeout_s=900,
        )
        if completed.returncode != 0:
            raise RepoUnavailable(
                f"could not check out {commit[:12]} of {slug}: {completed.stderr.strip()[:400]}"
            )
        ensure_readable(path)
    log.debug("worktree.created", slug=slug, commit=commit[:12])
    return path


def merge_base(slug: str, base: str, head: str, *, url: str | None = None) -> str | None:
    """Where `head` branched from `base`: the commit a pull request's change is
    measured from. None when the two share no history.

    GitHub reports a pull request's base as the base branch's tip, and the tip
    moves on after the branch is cut. Measured against the tip, everything merged
    since reads as the pull request undoing it. GitHub's own "Files changed" is
    measured from here instead.
    """
    assert_safe_revision(base)
    assert_safe_revision(head)
    bare = bare_clone(slug, url=url)
    for commit in (base, head):
        with _lock_for(f"commit:{slug}:{commit}"):
            _ensure_commit(bare, slug, commit, url)
    completed = _git(["merge-base", base, head], cwd=bare)
    found = completed.stdout.strip()
    return found if completed.returncode == 0 and found else None


def _ensure_commit(bare: Path, slug: str, commit: str, url: str | None) -> None:
    """Fetch if `commit` is not already in the clone."""
    if _git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=bare).returncode != 0:
        _git(["fetch", "origin", "--quiet", "--filter=blob:none"], cwd=bare, timeout_s=900,
             remote=url or f"https://github.com/{slug}")


def ensure_readable(path: Path) -> None:
    """Make `path` readable by the unprivileged user inside the container.

    Containers run `--user 1000:1000`. A tree created by a differently-numbered
    user — root in a container-based deployment, or any user whose umask is
    strict — is mounted successfully and then cannot be read at all, which shows
    up as a `PermissionError` on an unrelated-looking file rather than as a
    permissions problem. Widening read and traverse bits costs nothing: the
    mount is `:ro`, so nothing in the container can write here regardless.
    """
    try:
        for root, directories, files in os.walk(path):
            _widen(Path(root), directory=True)
            for name in directories:
                _widen(Path(root) / name, directory=True)
            for name in files:
                _widen(Path(root) / name, directory=False)
    except OSError as error:  # pragma: no cover - best effort
        log.warning("worktree.chmod_failed", path=str(path), error=str(error))


def _widen(path: Path, *, directory: bool) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return
    if stat.S_ISLNK(mode):
        return
    wanted = mode | stat.S_IRGRP | stat.S_IROTH
    # Directories need traverse; files only if the owner could already execute.
    if directory or mode & stat.S_IXUSR:
        wanted |= stat.S_IXGRP | stat.S_IXOTH
    if wanted != mode:
        with contextlib.suppress(OSError):
            path.chmod(wanted)


def _git(
    argv: Sequence[str], *, cwd: Path | None, timeout_s: float = 300, remote: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git. `remote` is the URL the command talks to, so a token can go with it
    (see `vcs.credentials`); commands that stay local leave it None."""
    command = ["git"] if cwd is None else ["git", f"--git-dir={cwd}"]
    try:
        return run_git([*command, *argv], remote=remote, timeout_s=timeout_s)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=[*command, *argv], returncode=124, stdout="", stderr="git timed out"
        )
