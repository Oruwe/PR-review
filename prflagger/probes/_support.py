"""Shared plumbing for the probes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from prflagger.brain.norms import declarative_norms
from prflagger.models import Norm

__all__ = ["SEVERITY", "files_at", "norm_for", "read_blob"]

# From SPEC.md § C9. behavior_change is 1.0 and lives in the differential.
SEVERITY = {
    "api_change": 0.8,
    "coverage_gap": 0.5,
    "lint_regression": 0.3,
}

# Which declared standard makes each probe's observation matter.
_NORM_ID = {
    "coverage_gap": "declared-tests-exist",
    "api_change": "declared-changelog-for-api",
    "lint_regression": "declared-ruff-clean",
    "lint_regression_types": "declared-types-clean",
}


def norm_for(kind: str, repo: Path) -> Norm | None:
    """The repo's own declared standard for this kind of observation.

    None when the repo declares nothing relevant — in which case the probe emits
    nothing, because a finding without a norm is not a finding.
    """
    wanted = _NORM_ID.get(kind)
    if wanted is None:
        return None
    for norm in declarative_norms(repo):
        if norm.id == wanted:
            return norm
    return None


def files_at(repo: Path, revision: str, relative_root: str) -> list[str]:
    """Python files under `relative_root` as of `revision`."""
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", revision, "--", relative_root],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in completed.stdout.splitlines() if line.endswith(".py")]


def read_blob(repo: Path, revision: str, relative: str) -> str | None:
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "show", f"{revision}:{relative}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None
