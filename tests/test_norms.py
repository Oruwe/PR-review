"""C7's clustering logic, and the harvest pagination parser.

Neither needs a model or the network: `_agglomerate` is arithmetic over vectors, the
thresholds and the confidence formula are pure logic, and `_paged` is a parser. Only the
embedding and naming boundaries are stubbed, and the counters below prove the batching
rule the spec sets for cost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from prflagger.brain import harvest as harvest_module
from prflagger.brain import norms as norms_module
from prflagger.brain.norms import (
    SIMILARITY_THRESHOLD,
    build_profile,
    cluster_norms,
    declarative_norms,
    detect_declared,
    profile_path,
    write_profile,
)

# Two clearly separated directions, plus a third far from both.
TESTY = [1.0, 0.0, 0.0]
TESTY_NEAR = [0.98, 0.14, 0.0]
STYLE = [0.0, 1.0, 0.0]
OTHER = [0.0, 0.0, 1.0]


def _comment(pr: int, reviewer: str, body: str) -> dict[str, Any]:
    return {"pr_number": pr, "reviewer_login": reviewer, "body": body, "diff_hunk": "@@"}


def _vectors_for(mapping: dict[str, list[float]]):  # type: ignore[no-untyped-def]
    def embed(texts: list[str]) -> list[list[float]]:
        return [mapping.get(text, OTHER) for text in texts]

    return embed


# --------------------------------------------------------------------------------------
# _agglomerate
# --------------------------------------------------------------------------------------


def test_similar_vectors_cluster_and_dissimilar_ones_do_not() -> None:
    clusters = norms_module._agglomerate(
        [TESTY, TESTY_NEAR, STYLE], SIMILARITY_THRESHOLD
    )
    grouped = sorted(sorted(cluster) for cluster in clusters)
    assert grouped == [[0, 1], [2]]


def test_a_threshold_of_one_keeps_everything_apart() -> None:
    clusters = norms_module._agglomerate([TESTY, TESTY_NEAR, STYLE], 1.0)
    assert sorted(sorted(c) for c in clusters) == [[0], [1], [2]]


def test_a_low_threshold_collapses_everything() -> None:
    clusters = norms_module._agglomerate([TESTY, TESTY_NEAR, STYLE], -1.0)
    assert len(clusters) == 1


def test_no_vectors_is_no_clusters() -> None:
    assert norms_module._agglomerate([], SIMILARITY_THRESHOLD) == []


# --------------------------------------------------------------------------------------
# cluster_norms — thresholds, scoring, batching
# --------------------------------------------------------------------------------------


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Embedding and naming stubbed at their boundaries; clustering stays real."""
    seen: dict[str, list[str]] = {"prompts": []}

    def name(prompt: str, **kwargs: object) -> str:
        seen["prompts"].append(prompt)
        return "New public functions require a test in the same pull request."

    monkeypatch.setattr(norms_module, "complete", name)
    return seen


def test_a_supported_cluster_becomes_a_cited_norm(
    stubbed: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = ["needs a test", "add coverage", "please test this"]
    vectors = dict(zip(bodies, [TESTY, TESTY_NEAR, TESTY], strict=True))
    monkeypatch.setattr(norms_module, "embed", _vectors_for(vectors))
    comments = [
        _comment(412, "bob", bodies[0]),
        _comment(457, "carol", bodies[1]),
        _comment(490, "dave", bodies[2]),
    ]

    norms = cluster_norms(comments)

    assert len(norms) == 1
    norm = norms[0]
    assert norm.support == 3
    assert norm.distinct_reviewers == 3
    assert norm.evidence_prs == (412, 457, 490)
    assert norm.scope == "repo"
    assert norm.id == "new-public-functions-require-a-test"
    # confidence = min(1, support/10) * min(1, reviewers/4)
    assert norm.confidence == pytest.approx(0.3 * 0.75)


def test_one_loud_maintainer_is_a_preference_not_a_standard(
    stubbed: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = ["needs a test", "add coverage", "please test this"]
    vectors = dict(zip(bodies, [TESTY, TESTY_NEAR, TESTY], strict=True))
    monkeypatch.setattr(norms_module, "embed", _vectors_for(vectors))
    # Same three comments, all from one reviewer.
    comments = [_comment(412 + i, "bob", body) for i, body in enumerate(bodies)]

    assert cluster_norms(comments) == []


def test_a_cluster_below_min_support_is_dropped(
    stubbed: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = ["needs a test", "add coverage"]
    vectors = dict(zip(bodies, [TESTY, TESTY_NEAR], strict=True))
    monkeypatch.setattr(norms_module, "embed", _vectors_for(vectors))
    comments = [_comment(412, "bob", bodies[0]), _comment(457, "carol", bodies[1])]

    assert cluster_norms(comments) == []
    assert cluster_norms(comments, min_support=2) != []


def test_naming_costs_one_call_per_cluster_not_per_comment(
    stubbed: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec batches naming for a reason: per-comment calls blow the budget."""
    testy = [f"test comment {i}" for i in range(6)]
    style = [f"style comment {i}" for i in range(6)]
    mapping = {**{b: TESTY for b in testy}, **{b: STYLE for b in style}}
    monkeypatch.setattr(norms_module, "embed", _vectors_for(mapping))

    comments = [
        _comment(400 + i, f"r{i % 4}", body) for i, body in enumerate(testy + style)
    ]
    norms = cluster_norms(comments)

    assert len(norms) == 2, "two distinct standards"
    assert len(stubbed["prompts"]) == 2, (
        f"one call per cluster, got {len(stubbed['prompts'])} for {len(comments)} comments"
    )


def test_confidence_saturates_at_the_documented_ceilings(
    stubbed: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = [f"needs a test {i}" for i in range(12)]
    monkeypatch.setattr(norms_module, "embed", _vectors_for({b: TESTY for b in bodies}))
    comments = [_comment(400 + i, f"r{i % 5}", body) for i, body in enumerate(bodies)]

    norm = cluster_norms(comments)[0]
    assert norm.support == 12
    assert norm.distinct_reviewers == 5
    assert norm.confidence == pytest.approx(1.0)  # both terms capped at 1.0


def test_empty_comments_need_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> Any:
        raise AssertionError("must not call a model for an empty corpus")

    monkeypatch.setattr(norms_module, "embed", explode)
    monkeypatch.setattr(norms_module, "complete", explode)
    assert cluster_norms([]) == []
    assert cluster_norms([_comment(1, "bob", "   ")]) == []


# --------------------------------------------------------------------------------------
# The declarative profile
# --------------------------------------------------------------------------------------


def test_detect_declared_reads_the_repos_real_configuration(head_worktree: Path) -> None:
    declared = detect_declared(head_worktree)
    assert declared["linter"] == "ruff"
    assert declared["type_checker"] == "mypy --strict"
    assert declared["test_runner"] == "pytest"
    assert declared["changelog"] == "CHANGES.md"


def test_a_repo_that_declares_nothing_yields_no_declarative_norms(tmp_path: Path) -> None:
    assert detect_declared(tmp_path) == {}
    assert declarative_norms(tmp_path) == []


def test_declarative_norms_carry_no_pr_evidence(head_worktree: Path) -> None:
    for norm in declarative_norms(head_worktree):
        assert norm.evidence_prs == (), "their evidence is the config file, not a review"
        assert norm.support == 0
        assert norm.confidence == 1.0, "nothing was inferred; the repo automated it"


def test_profile_round_trips(head_worktree: Path, tmp_path: Path) -> None:
    norms = declarative_norms(head_worktree)
    profile = build_profile("pallets/click", head_worktree, norms, prs_analyzed=180)

    assert profile["repo"] == "pallets/click"
    assert profile["prs_analyzed"] == 180
    assert profile["declared"]["linter"] == "ruff"
    assert len(profile["norms"]) == len(norms)
    assert profile["generated_at"]

    path = write_profile(profile, profile_path("pallets/click", tmp_path))
    assert json.loads(path.read_text(encoding="utf-8")) == profile
    assert "pallets__click" in str(path)


# --------------------------------------------------------------------------------------
# harvest pagination
# --------------------------------------------------------------------------------------


def test_paged_concatenates_the_pages_gh_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh api --paginate` emits one JSON array per page, back to back."""
    pages = json.dumps([{"number": 1}, {"number": 2}]) + json.dumps([{"number": 3}])

    def fake(argv: list[str]):  # type: ignore[no-untyped-def]
        import subprocess

        return subprocess.CompletedProcess(argv, 0, pages, "")

    monkeypatch.setattr(harvest_module, "_gh", fake)
    items = harvest_module._paged("repos/x/y/pulls", {}, 10)
    assert [item["number"] for item in items] == [1, 2, 3]


def test_paged_respects_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = json.dumps([{"number": n} for n in range(10)])

    def fake(argv: list[str]):  # type: ignore[no-untyped-def]
        import subprocess

        return subprocess.CompletedProcess(argv, 0, pages, "")

    monkeypatch.setattr(harvest_module, "_gh", fake)
    assert len(harvest_module._paged("repos/x/y/pulls", {}, 4)) == 4


def test_paged_reports_a_failure_instead_of_returning_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(argv: list[str]):  # type: ignore[no-untyped-def]
        import subprocess

        return subprocess.CompletedProcess(argv, 1, "", "HTTP 403 rate limit exceeded")

    monkeypatch.setattr(harvest_module, "_gh", fake)
    with pytest.raises(harvest_module.GhUnavailable, match="403"):
        harvest_module._paged("repos/x/y/pulls", {}, 10)
