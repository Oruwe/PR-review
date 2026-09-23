"""All shared domain types.

Every type here is a frozen dataclass or an enum. Nothing in this module is
redefined elsewhere: components import from here.

v1's types (`Symbol`, `Outcome`, `Job`, `TestResult`, `Norm`, `Finding`) keep
their exact shape — `prflagger.models` re-exports them so existing imports and
tests keep working. What v2 adds is the vocabulary an always-on service needs:
a run with a state machine, the observations probes emit before anything has
judged them, and the adjudication that may later annotate one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum

__all__ = [
    "Adjudication",
    "Citation",
    "Finding",
    "Job",
    "LogLine",
    "Norm",
    "Observation",
    "Outcome",
    "PullRequest",
    "Repo",
    "ResourceSample",
    "Run",
    "RunState",
    "Suggestion",
    "Symbol",
    "TestResult",
    "TERMINAL_STATES",
]


# ----------------------------------------------------------------------------------
# v1 types — shape pinned by SPEC.md, do not change
# ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """A named definition located in a repo under analysis."""

    fqn: str  # "pkg.module.Class.method"
    kind: str  # "function" | "method" | "class"
    file: str  # repo-relative posix path
    line_start: int
    line_end: int
    lang: str = "python"


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


# ----------------------------------------------------------------------------------
# v2 — what an always-on service needs
# ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Repo:
    """A repository the service watches."""

    slug: str  # "owner/name"
    default_branch: str = "main"
    toolchain_id: str = ""  # "" means: detect it
    package_roots: tuple[str, ...] = ()
    added_at: float = 0.0
    atlas_sha: str = ""  # sha the current atlas was built from

    @property
    def owner(self) -> str:
        return self.slug.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.slug.split("/", 1)[-1]


@dataclass(frozen=True)
class PullRequest:
    """A pull request as GitHub last reported it."""

    repo: str
    number: int
    title: str
    body: str
    author: str
    base_sha: str
    head_sha: str
    state: str  # "open" | "closed" | "merged"
    updated_at: str  # ISO-8601, as GitHub returns it
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    draft: bool = False


class RunState(str, Enum):  # noqa: UP042 — mirrors Outcome's string-enum shape
    """Where a run has got to. Persisted, so the value strings are an interface."""

    QUEUED = "queued"
    PREPARING = "preparing"
    IMAGE_BUILD = "image_build"
    BASE_RUN = "base_run"
    HEAD_RUN = "head_run"
    PROBING = "probing"
    ADJUDICATING = "adjudicating"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: A run in any other state is in flight, and is requeued on boot by `engine.recovery`.
TERMINAL_STATES = frozenset({RunState.DONE, RunState.FAILED, RunState.CANCELLED})


@dataclass(frozen=True)
class Run:
    """One verification of one PR."""

    id: str
    repo: str
    pr_number: int
    base_sha: str
    head_sha: str
    state: RunState = RunState.QUEUED
    trigger: str = "manual"  # "manual" | "watcher" | "webhook"
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""
    usd_spent: float = 0.0


@dataclass(frozen=True)
class LogLine:
    """One line of output from a sandboxed process.

    `offset_ms` is measured from the job's start rather than wall-clock, so a
    replayed transcript has the same timing as the live one.
    """

    run_id: str
    job_id: str
    stream: str  # "stdout" | "stderr" | "meta"
    seq: int
    offset_ms: int
    text: str


@dataclass(frozen=True)
class ResourceSample:
    """One reading of a running container's resource use."""

    job_id: str
    offset_ms: int
    cpu_pct: float
    rss_mb: int
    pids: int


@dataclass(frozen=True)
class Observation:
    """A mechanical fact a probe found. Pre-adjudication: nothing has judged it.

    This is what probes emit. A `Finding` is an Observation that has been ranked
    and had a norm attached; an `Adjudication` may then annotate it. Keeping them
    distinct is what stops an LLM from being able to invent a finding — it can
    only ever speak about an Observation that a probe already produced.
    """

    id: str
    run_id: str
    kind: str
    symbol: str
    what_changed: str
    how_we_know: str
    evidence_ref: str = ""  # nodeid, file:line, or diff hunk id
    severity: float = 0.5
    confidence: float = 0.5
    rank_score: float = 0.0

    def __post_init__(self) -> None:
        if not self.what_changed:
            raise ValueError("Observation.what_changed is required")
        if not self.how_we_know:
            raise ValueError("Observation.how_we_know is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Observation.confidence must be 0.0-1.0, got {self.confidence}")
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError(f"Observation.severity must be 0.0-1.0, got {self.severity}")


@dataclass(frozen=True)
class Citation:
    """Where an adjudication's claim comes from.

    `ref` must resolve against a real artifact — a norm id that exists, a
    `path:line` that exists at the run's sha, a nodeid the run actually produced.
    `adjudicate.citations` checks this; an unresolvable citation voids the
    adjudication that carried it.
    """

    type: str  # "norm" | "code" | "test" | "diff"
    ref: str
    quote: str = ""

    _TYPES = ("norm", "code", "test", "diff")

    def __post_init__(self) -> None:
        if self.type not in self._TYPES:
            raise ValueError(f"Citation.type must be one of {self._TYPES}, got {self.type!r}")
        if not self.ref:
            raise ValueError(
                "Citation.ref is required — a citation with nothing to check is not one"
            )


@dataclass(frozen=True)
class Adjudication:
    """The rethink: this observation, read against the repo's own code and norms.

    Never a verdict on the PR. An adjudication carrying no citation raises here,
    so the "every claim is checkable" property is enforced by the type rather
    than by a code path someone can forget.
    """

    observation_id: str
    assessment: str
    reasoning: str
    citations: tuple[Citation, ...] = ()
    model: str = ""
    usd: float = 0.0

    ASSESSMENTS = (
        "consistent_with_repo",
        "diverges_from_repo",
        "insufficient_evidence",
        "probe_false_positive",
    )

    def __post_init__(self) -> None:
        if self.assessment not in self.ASSESSMENTS:
            raise ValueError(
                f"Adjudication.assessment must be one of {self.ASSESSMENTS}, "
                f"got {self.assessment!r}"
            )
        if not self.citations:
            raise ValueError(
                "Adjudication must carry at least one citation; an uncited judgement "
                "is an opinion, and this system does not emit opinions"
            )
        if not self.reasoning:
            raise ValueError("Adjudication.reasoning is required")


@dataclass(frozen=True)
class Suggestion:
    """A concrete change, attached to one observation, backed by citations."""

    observation_id: str
    summary: str
    rationale: str
    patch_sketch: str = ""
    confidence: float = 0.5
    citations: tuple[Citation, ...] = ()

    def __post_init__(self) -> None:
        if not self.summary:
            raise ValueError("Suggestion.summary is required")
        if not self.citations:
            raise ValueError(
                "Suggestion must carry at least one citation — see CLAUDE.md, "
                "suggestions are permitted only when they cite evidence"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Suggestion.confidence must be 0.0-1.0, got {self.confidence}")
