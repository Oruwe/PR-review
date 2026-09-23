"""Measuring a repository: what is in it, how big, how often it changes.

All of this comes from the filesystem and `git`, never from a model. CLAUDE.md
is explicit that structure is derived, not generated, and churn in particular is
the kind of number that would be quietly wrong forever if it were guessed.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import structlog

__all__ = ["Churn", "FileFact", "inventory", "churn_by_path", "language_of", "module_of"]

log = structlog.get_logger(__name__)

#: Extension -> language. Deliberately small: a language nobody can analyse is
#: still worth counting, but inventing entries for every extension on earth adds
#: noise without adding information.
_LANGUAGES = {
    ".py": "Python", ".pyi": "Python",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++",
    ".cs": "C#", ".php": "PHP", ".swift": "Swift", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Shell",
    ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "CSS",
    ".md": "Markdown", ".rst": "Markdown",
    ".yml": "Config", ".yaml": "Config", ".toml": "Config", ".json": "Config", ".ini": "Config",
}

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".nox", "dist", "build", "target", "vendor", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".idea", ".vscode", "site-packages", ".next", ".cache", "coverage",
    "htmlcov", ".gradle", "Pods",
}

#: Beyond this a file is almost certainly generated or vendored; counting it
#: would swamp every real module in the treemap.
_MAX_LINES = 50_000


@dataclass(frozen=True)
class FileFact:
    path: str  # repo-relative posix
    language: str
    loc: int
    is_test: bool
    bytes_: int


@dataclass
class Churn:
    commits: int = 0
    insertions: int = 0
    deletions: int = 0
    authors: set[str] = field(default_factory=set)

    @property
    def touched(self) -> int:
        return self.insertions + self.deletions


def language_of(path: str) -> str:
    return _LANGUAGES.get(Path(path).suffix.lower(), "Other")


def module_of(path: str, *, depth: int = 2) -> str:
    """The grouping a file belongs to: its directory, capped at `depth`.

    Capping matters for the treemap. A repo nested eight levels deep otherwise
    produces hundreds of one-file "modules" and the picture says nothing.
    """
    parts = Path(path).parts[:-1]
    if not parts:
        return "(root)"
    return "/".join(parts[:depth])


def _is_test(path: str, globs: tuple[str, ...]) -> bool:
    lowered = path.lower()
    if any(fnmatch(lowered, g.lower()) for g in globs):
        return True
    parts = Path(lowered).parts
    return any(part in ("test", "tests", "__tests__", "spec", "specs") for part in parts)


def inventory(
    repo_path: Path, *, test_globs: tuple[str, ...] = (), max_files: int = 20_000
) -> list[FileFact]:
    """Every source file in the repo, with its language, size and test status."""
    facts: list[FileFact] = []
    for path in _walk(repo_path, max_files):
        relative = path.relative_to(repo_path).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 4 * 1024 * 1024:
            continue  # a multi-megabyte source file is data, not code
        loc = _count_lines(path)
        if loc > _MAX_LINES:
            continue
        facts.append(
            FileFact(
                path=relative,
                language=language_of(relative),
                loc=loc,
                is_test=_is_test(relative, test_globs),
                bytes_=size,
            )
        )
    return facts


def _walk(root: Path, max_files: int) -> list[Path]:
    found: list[Path] = []
    stack = [root]
    while stack and len(found) < max_files:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if _descend(entry.name):
                    stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() in _LANGUAGES:
                found.append(entry)
                if len(found) >= max_files:
                    break
    return found


def _descend(name: str) -> bool:
    """Whether to walk into a directory.

    Dotted directories are skipped except `.github`: a repo's workflows are
    part of how it builds itself, and the generic pack reads them.
    """
    if name == ".github":
        return True
    return name not in _SKIP_DIRS and not name.startswith(".")


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def churn_by_path(
    repo_path: Path, *, days: int = 365, max_commits: int = 4000
) -> dict[str, Churn]:
    """How often each file changed, from `git log --numstat`.

    A window rather than all history: a file rewritten constantly two years ago
    and untouched since is not a hotspot today, and treating it as one buries the
    files that are actually moving.
    """
    completed = subprocess.run(  # noqa: S603
        [
            "git", "-C", str(repo_path), "log", f"--since={days}.days.ago",
            f"--max-count={max_commits}", "--numstat", "--no-renames",
            "--pretty=format:%x01%an",
        ],
        capture_output=True, text=True, check=False, timeout=180,
    )
    if completed.returncode != 0:
        log.warning("churn.unavailable", error=completed.stderr.strip()[:200])
        return {}

    churn: dict[str, Churn] = defaultdict(Churn)
    author = ""
    seen_this_commit: set[str] = set()
    for line in completed.stdout.splitlines():
        if line.startswith("\x01"):
            author = line[1:].strip()
            seen_this_commit = set()
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        entry = churn[path]
        if path not in seen_this_commit:
            entry.commits += 1
            seen_this_commit.add(path)
        if author:
            entry.authors.add(author)
        entry.insertions += int(added) if added.isdigit() else 0
        entry.deletions += int(removed) if removed.isdigit() else 0
    return dict(churn)
