"""Backwards-compatible re-export of the domain types.

The types moved to `prflagger.core.models` when v2 split the package into
layers. This module stays so existing imports keep working; it defines nothing
of its own, which is what `CLAUDE.md`'s "never redefined elsewhere" rule
requires.
"""

from __future__ import annotations

from prflagger.core.models import (
    Adjudication,
    Citation,
    Finding,
    Job,
    LogLine,
    Norm,
    Observation,
    Outcome,
    PullRequest,
    Repo,
    ResourceSample,
    Run,
    RunState,
    Suggestion,
    Symbol,
    TestResult,
)

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
]
