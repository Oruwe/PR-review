"""All shared domain types.

Every type in this module is a frozen dataclass (or an enum). Nothing here is
redefined elsewhere in the codebase: components import from here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum

__all__ = [
    "Finding",
    "Job",
    "Norm",
    "Outcome",
    "Symbol",
    "TestResult",
]


@dataclass(frozen=True)
class Symbol:
    """A named definition located in the target repo."""

    fqn: str  # "pkg.module.Class.method"
    kind: str  # "function" | "method" | "class"
    file: str  # repo-relative posix path
    line_start: int
    line_end: int


class Outcome(str, Enum):  # noqa: UP042 — the shape is pinned by SPEC.md
    """The typed result of a sandbox run. Never raised — always returned."""

    PASSED = "passed"
    FAILED = "failed"
    INSTALL_FAILED = "install_failed"
    TIMEOUT = "timeout"
    COLLECTION_ERROR = "collection_error"
    OOM = "oom"


@dataclass(frozen=True)
class Job:
    """One unit of sandboxed work: a command run against a commit in an image."""

    repo_path: str
    commit: str
    image_key: str
    command: tuple[str, ...]
    timeout_s: int = 120
    memory_mb: int = 512

    @property
    def idempotency_key(self) -> str:
        """sha256 of the canonical JSON of all fields. Stable across runs."""
        canonical = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=_json_default,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: object) -> object:
    """Canonicalise values json does not serialise natively."""
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not canonically serialisable: {type(value).__name__}")


@dataclass(frozen=True)
class TestResult:
    """What a sandbox run observed. `outcome` carries failure modes, including OOM."""

    outcome: Outcome
    per_test: dict[str, str]  # pytest nodeid -> "passed"|"failed"|"error"
    duration_s: float
    peak_rss_mb: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Norm:
    """A repo standard mined from enforced review history."""

    id: str  # kebab-case slug
    statement: str  # imperative, one line
    scope: str  # "repo" | "project" | "org"
    support: int
    distinct_reviewers: int
    confidence: float  # 0.0-1.0
    evidence_prs: tuple[int, ...]


@dataclass(frozen=True)
class Finding:
    """An observation with evidence. Never a verdict.

    The four-field contract: `what_changed`, `how_we_know`, `norm` and
    `confidence` must all be carried or the finding is not emitted.
    """

    kind: str  # "behavior_change"|"coverage_gap"|"lint_regression"|
    # "api_change"|"timeout"|"oom"
    symbol: str  # fqn
    what_changed: str
    how_we_know: str  # nodeid, number, or diff hunk
    norm: Norm | None  # None permitted ONLY when kind == "behavior_change"
    confidence: float
    severity: float  # 0.0-1.0, from probe type

    def __post_init__(self) -> None:
        """Raise ValueError if norm is None and kind != 'behavior_change'."""
        if self.norm is None and self.kind != "behavior_change":
            raise ValueError(
                f"Finding of kind {self.kind!r} must cite a norm; "
                "norm=None is permitted only for kind='behavior_change'"
            )
        if not self.what_changed:
            raise ValueError("Finding.what_changed is required by the four-field contract")
        if not self.how_we_know:
            raise ValueError("Finding.how_we_know is required by the four-field contract")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Finding.confidence must be in 0.0-1.0, got {self.confidence}")
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError(f"Finding.severity must be in 0.0-1.0, got {self.severity}")
