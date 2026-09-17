"""Ordering findings.

The agent issues no verdicts, but it must decide order: forty findings is as useless as
none, because it hands the bandwidth problem straight back.

The formula is arithmetic and contains no LLM, so a maintainer can be shown exactly why
a finding ranked first and can tune it for their repo.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import structlog

from prflagger.brain.store import KnowledgeStore
from prflagger.models import Finding, Norm

__all__ = ["MATCH_THRESHOLD", "SCOPE_WEIGHT", "rank", "score"]

log = structlog.get_logger(__name__)

# Below this the match is not good enough to cite, and a citation nobody can check is
# worse than none.
MATCH_THRESHOLD = 0.6

SCOPE_WEIGHT = {"repo": 1.0, "project": 0.8, "org": 0.6}


def rank(findings: list[Finding], norms: list[Norm], store: KnowledgeStore) -> list[Finding]:
    """Attach the best-matching norm to each finding via store.match_norm (drop the
    match below similarity 0.6). Then sort by:
    score = confidence * severity * centrality * scope_weight.
    No LLM in this function."""
    repo = _repo_slug()
    attached = [_attach(finding, store, repo) for finding in findings]

    caller_counts = {
        finding.symbol: len(store.callers_of(repo, finding.symbol, 1)) for finding in attached
    }
    widest = max(caller_counts.values(), default=0)

    scored = [
        (score(finding, caller_counts.get(finding.symbol, 0), widest), finding)
        for finding in attached
    ]
    # Every tiebreaker is content, so the same input gives the same order twice.
    scored.sort(key=lambda pair: (-pair[0], pair[1].symbol, pair[1].kind, pair[1].how_we_know))
    return [finding for _, finding in scored]


def score(finding: Finding, caller_count: int, widest: int) -> float:
    return (
        finding.confidence
        * finding.severity
        * centrality(caller_count, widest)
        * SCOPE_WEIGHT.get(finding.norm.scope if finding.norm else "repo", 1.0)
    )


def centrality(caller_count: int, widest: int) -> float:
    """Normalised caller count.

    The +1 matters: a brand-new public function has no callers yet, and a bare ratio
    would multiply its score to zero — sinking exactly the finding worth reading.
    """
    return (1 + caller_count) / (1 + widest) if widest > 0 else 1.0


def _attach(finding: Finding, store: KnowledgeStore, repo: str) -> Finding:
    """Best-matching norm above the threshold, else whatever the probe already cited."""
    matches = store.match_norm(repo, f"{finding.what_changed} {finding.how_we_know}", k=3)
    best = next((pair for pair in matches if pair[1] >= MATCH_THRESHOLD), None)
    if best is None:
        return finding
    from dataclasses import replace

    return replace(finding, norm=best[0])


def _repo_slug() -> str:
    config = Path("config.toml")
    if not config.is_file():
        return ""
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    return str(data.get("target", {}).get("slug", ""))
