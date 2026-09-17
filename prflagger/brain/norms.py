"""Norms: what this repository has historically required.

Two sources, very different in value.

Declarative — the linter and type-checker config. Reliable, but it only tells you what
the project already automated, and a linter's rules are the standards nobody needs a
human for.

Review history — the actual prize. Recurring standards mined from comments that were
*enforced*, each carrying the PR numbers it came from. Those are the standards no linter
catches, which is why humans were still reviewing them.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from prflagger.llm import complete, embed
from prflagger.models import Norm
from prflagger.providers import DEFAULT_MODEL

__all__ = [
    "build_profile",
    "cluster_norms",
    "declarative_norms",
    "detect_declared",
    "profile_path",
    "write_profile",
]

log = structlog.get_logger(__name__)

SIMILARITY_THRESHOLD = 0.75

_NAMING_PROMPT = """\
Here are {count} code review comments that a clustering step grouped together because
they say similar things.

{comments}

Write the single standard they are all enforcing, as one imperative sentence a
maintainer would recognise. Be specific to what these comments actually demand — "write
good code" is useless. Do not hedge, do not explain.

Return only the sentence.
"""


# ----------------------------------------------------------------------------------
# Declarative: what the repo already automates
# ----------------------------------------------------------------------------------


def detect_declared(repo: Path) -> dict[str, str]:
    """The linter and type-checker configuration the repo declares for itself."""
    declared: dict[str, str] = {}
    pyproject = repo / "pyproject.toml"
    tools: dict[str, Any] = {}
    if pyproject.is_file():
        try:
            tools = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
        except (OSError, tomllib.TOMLDecodeError):
            tools = {}

    if "ruff" in tools or (repo / "ruff.toml").is_file() or (repo / ".ruff.toml").is_file():
        declared["linter"] = "ruff"
    elif (repo / ".flake8").is_file() or "flake8" in tools:
        declared["linter"] = "flake8"

    mypy = tools.get("mypy", {})
    if mypy or (repo / "mypy.ini").is_file() or (repo / ".mypy.ini").is_file():
        declared["type_checker"] = "mypy --strict" if mypy.get("strict") else "mypy"
    elif "pyright" in tools:
        declared["type_checker"] = "pyright"

    if "pytest" in tools or (repo / "pytest.ini").is_file() or (repo / "tox.ini").is_file():
        declared["test_runner"] = "pytest"

    for name in ("CHANGES.md", "CHANGELOG.md", "CHANGES.rst", "CHANGELOG.rst"):
        if (repo / name).is_file():
            declared["changelog"] = name
            break
    return declared


def declarative_norms(repo: Path) -> list[Norm]:
    """Norms the repo states about itself, rather than ones mined from its history.

    These carry no `evidence_prs`: their evidence is the config file, not a review. They
    are high confidence precisely because nothing was inferred — the project automated
    the rule itself.
    """
    declared = detect_declared(repo)
    norms: list[Norm] = []
    if "linter" in declared:
        norms.append(
            Norm(
                id=f"declared-{declared['linter']}-clean",
                statement=(
                    f"Code must pass the repository's own {declared['linter']} "
                    "configuration."
                ),
                scope="repo",
                support=0,
                distinct_reviewers=0,
                confidence=1.0,
                evidence_prs=(),
            )
        )
    if "type_checker" in declared:
        norms.append(
            Norm(
                id="declared-types-clean",
                statement=(
                    f"Code must type-check under the repository's own "
                    f"{declared['type_checker']} configuration."
                ),
                scope="repo",
                support=0,
                distinct_reviewers=0,
                confidence=1.0,
                evidence_prs=(),
            )
        )
    if "changelog" in declared:
        norms.append(
            Norm(
                id="declared-changelog-for-api",
                statement=(
                    "Changes to the public API must be recorded in "
                    f"{declared['changelog']}."
                ),
                scope="repo",
                support=0,
                distinct_reviewers=0,
                confidence=1.0,
                evidence_prs=(),
            )
        )
    if "test_runner" in declared:
        norms.append(
            Norm(
                id="declared-tests-exist",
                statement=(
                    "New code must be exercised by the repository's own test suite."
                ),
                scope="repo",
                support=0,
                distinct_reviewers=0,
                confidence=1.0,
                evidence_prs=(),
            )
        )
    return norms


# ----------------------------------------------------------------------------------
# Mined: what reviewers actually enforced
# ----------------------------------------------------------------------------------


def cluster_norms(
    comments: list[dict[str, Any]],
    *,
    min_support: int = 3,
    min_reviewers: int = 2,
) -> list[Norm]:
    """Embed comment bodies, cluster by cosine similarity (threshold 0.75,
    agglomerative), then for each cluster of size >= min_support ask the LLM for one
    imperative statement. Drop clusters below either threshold.
    evidence_prs = the cluster's PR numbers."""
    bodies = [str(comment.get("body", "")).strip() for comment in comments]
    keep = [index for index, body in enumerate(bodies) if body]
    if not keep:
        return []

    vectors = embed([bodies[index] for index in keep])
    clusters = _agglomerate(vectors, SIMILARITY_THRESHOLD)

    norms: list[Norm] = []
    for cluster in clusters:
        members = [comments[keep[position]] for position in cluster]
        reviewers = {str(member.get("reviewer_login", "")) for member in members} - {""}
        support = len(members)
        if support < min_support or len(reviewers) < min_reviewers:
            continue

        statement = _name_cluster([str(member.get("body", "")) for member in members])
        if not statement:
            continue
        pr_numbers = sorted(
            {int(member["pr_number"]) for member in members if "pr_number" in member}
        )
        norms.append(
            Norm(
                id=_slug(statement),
                statement=statement,
                scope="repo",
                support=support,
                distinct_reviewers=len(reviewers),
                # One loud maintainer is a preference; four people is a standard.
                confidence=min(1.0, support / 10) * min(1.0, len(reviewers) / 4),
                evidence_prs=tuple(pr_numbers),
            )
        )
    log.info("clustered", clusters=len(clusters), norms=len(norms))
    return norms


def _agglomerate(vectors: Sequence[Sequence[float]], threshold: float) -> list[list[int]]:
    """Average-linkage agglomerative clustering over cosine similarity."""
    import numpy as np

    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.size == 0:
        return []
    normalised = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12)
    similarity = normalised @ normalised.T

    clusters: list[list[int]] = [[index] for index in range(len(normalised))]
    while len(clusters) > 1:
        best = (-1.0, -1, -1)
        for left in range(len(clusters)):
            for right in range(left + 1, len(clusters)):
                score = float(
                    similarity[np.ix_(clusters[left], clusters[right])].mean()
                )
                if score > best[0]:
                    best = (score, left, right)
        if best[0] < threshold:
            break
        _, left, right = best
        clusters[left] = clusters[left] + clusters[right]
        del clusters[right]
    return [sorted(cluster) for cluster in clusters]


def _name_cluster(bodies: Sequence[str]) -> str:
    """One LLM call per cluster, never per comment."""
    joined = "\n\n".join(f"- {body.strip()}" for body in bodies[:25])
    response = complete(
        _NAMING_PROMPT.format(count=len(bodies), comments=joined),
        model=DEFAULT_MODEL,
        max_tokens=256,
    )
    return response.strip().strip('"').splitlines()[0].strip() if response.strip() else ""


def _slug(statement: str) -> str:
    words = re.findall(r"[a-z0-9]+", statement.lower())
    return "-".join(words[:6]) or "norm"


# ----------------------------------------------------------------------------------
# The profile
# ----------------------------------------------------------------------------------


def profile_path(repo_slug: str, cache_root: Path) -> Path:
    return cache_root / "brain" / repo_slug.replace("/", "__") / "repo_profile.json"


def build_profile(
    repo_slug: str, repo: Path, norms: list[Norm], *, prs_analyzed: int
) -> dict[str, Any]:
    return {
        "repo": repo_slug,
        "generated_at": datetime.now(UTC).date().isoformat(),
        "prs_analyzed": prs_analyzed,
        "norms": [
            {
                "id": norm.id,
                "statement": norm.statement,
                "scope": norm.scope,
                "support": norm.support,
                "distinct_reviewers": norm.distinct_reviewers,
                "confidence": norm.confidence,
                "evidence_prs": list(norm.evidence_prs),
            }
            for norm in norms
        ],
        "declared": detect_declared(repo),
    }


def write_profile(profile: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, sort_keys=True), encoding="utf-8")
    return path
