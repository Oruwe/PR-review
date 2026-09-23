"""Adjudication: a model reads each observation against the repository, and
nothing it says is kept unless every reference in it checks out.

Real git worktrees, a real database, the real Anthropic SDK over real HTTP to a
local server that plays the model. The model's answers are scripted; what is
under test is everything around them — what it is shown, and what survives.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from prflagger.adjudicate.adjudicate import adjudicate
from prflagger.adjudicate.citations import Evidence, resolve
from prflagger.adjudicate.situate import changed_paths, group, repo_context
from prflagger.core.config import BudgetConfig, Config, ModelConfig
from prflagger.core.models import Citation, Norm, Observation, PullRequest, Repo
from prflagger.llm.client import build_client
from prflagger.llm.provider import bedrock_mantle
from prflagger.storage.db import Database
from prflagger.storage.repos import Store
from prflagger.vcs.worktrees import worktree_for
from tests.adjudication_fixture import fabricating, honest
from tests.model_fixture import RecordedModel, message, serve
from tests.v2_fixtures import REGRESSION_TEST, build_repo

SLUG = "demo/shoplib"


@pytest.fixture
def trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str, str]:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    repo, base, head = build_repo(tmp_path / "repo")
    return (worktree_for(SLUG, base, url=str(repo)), worktree_for(SLUG, head, url=str(repo)),
            base, head)


def _evidence(trees: tuple[Path, Path, str, str]) -> Evidence:
    base_tree, head_tree, base, head = trees
    return Evidence(
        head_tree=head_tree, base_tree=base_tree,
        norm_ids=frozenset({"declared-tests"}),
        test_ids=frozenset({REGRESSION_TEST}),
        changed_paths=frozenset(changed_paths(head_tree, base, head)),
        charter_sources={"README.md:3": "Price shopping carts correctly."},
    )


# ----------------------------------------------------------------------------------
# Every reference is checked against something real
# ----------------------------------------------------------------------------------


def test_references_resolve_only_against_what_exists(
    trees: tuple[Path, Path, str, str],
) -> None:
    evidence = _evidence(trees)
    ok = [
        Citation("code", "shoplib/pricing.py:6", "if percent < 0:"),
        Citation("code", "base:shoplib/pricing.py:6", "if percent <= 0:"),
        Citation("code", "shoplib/pricing.py:4-6"),
        Citation("code", "README.md:3", "Price shopping carts"),
        Citation("norm", "declared-tests"),
        Citation("test", REGRESSION_TEST),
        Citation("diff", "shoplib/pricing.py"),
    ]
    for citation in ok:
        assert resolve(citation, evidence) is not None, citation
    assert resolve(Citation("code", "shoplib/pricing.py:4-6"), evidence).quote  # type: ignore[union-attr]

    fabricated = [
        Citation("code", "shoplib/pricing.py:6", "if percent <= 0:"),  # that is the base
        Citation("code", "shoplib/pricing.py:9999"),
        Citation("code", "../../etc/passwd:1"),
        Citation("code", "/etc/passwd:1"),
        Citation("code", "shoplib/pricing.py:1-400"),
        Citation("code", "shoplib/missing.py:1"),
        Citation("code", "README.md:3", "Price shopping carts badly."),
        Citation("norm", "review-always-use-decimal"),
        Citation("test", "tests/test_pricing.py::test_that_never_ran"),
        Citation("diff", "tests/test_pricing.py"),  # this pull request did not change it
    ]
    for citation in fabricated:
        assert resolve(citation, evidence) is None, citation


# ----------------------------------------------------------------------------------
# What the model is shown, and what survives its answer
# ----------------------------------------------------------------------------------


def _setup(tmp_path: Path, trees: tuple[Path, Path, str, str]
           ) -> tuple[Store, list[Observation], PullRequest]:
    store = Store(Database(tmp_path / "adj.db"))
    store.put_repo(Repo(slug=SLUG, added_at=time.time()))
    store.replace_norms(SLUG, "declared", [Norm(
        id="declared-tests", statement="Changes keep the repository's own test suite passing.",
        scope="repo", support=0, distinct_reviewers=0, confidence=1.0, evidence_prs=(),
        source="declared", evidence=((0, "", "tests/test_pricing.py:1"),), named_by="config",
    )])
    observations = [
        Observation(id="o-behaviour", run_id="r1", kind="behavior_change",
                    symbol="test_negative_discount_is_identity",
                    what_changed="passed at base, failed at head",
                    how_we_know=f"nodeid {REGRESSION_TEST}: passed -> failed",
                    evidence_ref=REGRESSION_TEST, severity=1.0, confidence=0.9, rank_score=0.9,
                    norm_id="declared-tests"),
        Observation(id="o-api", run_id="r1", kind="api_change",
                    symbol="shoplib.pricing.bulk_total",
                    what_changed="public symbol shoplib.pricing.bulk_total was added",
                    how_we_know="absent at base, present at head in shoplib/pricing.py:16",
                    evidence_ref="shoplib/pricing.py:16", severity=0.6, confidence=0.9,
                    rank_score=0.5),
    ]
    pull = PullRequest(repo=SLUG, number=1, title="fix: validate the discount percentage",
                       body="Rejects a negative percent instead of ignoring it.", author="d",
                       base_sha=trees[2], head_sha=trees[3], state="open", updated_at="now")
    return store, observations, pull


def _run(tmp_path: Path, trees: tuple[Path, Path, str, str], respond: object,
         budget: BudgetConfig | None = None) -> tuple[object, RecordedModel]:
    store, observations, pull = _setup(tmp_path, trees)
    base_tree, head_tree, base, head = trees
    model = RecordedModel(respond)  # type: ignore[arg-type]
    with serve(model):
        client = build_client(
            Config(budget=budget or BudgetConfig(), models=ModelConfig()),
            Database(tmp_path / "spend.db"),
            provider=bedrock_mantle("us-east-1", base_url=model.base_url, skip_auth=True),
        )
        packets = group(observations, repo=SLUG, store=store, base_tree=base_tree,
                        head_tree=head_tree, base_sha=base, head_sha=head, limit=8)
        outcome = adjudicate(
            run_id="r1", repo=SLUG, pull=pull, packets=packets,
            context=repo_context(SLUG, None, store.norms(SLUG)),
            evidence=_evidence(trees), client=client, model="anthropic.claude-sonnet-5",
        )
    return outcome, model


def test_the_model_sees_numbered_source_the_diff_and_the_standards(
    tmp_path: Path, trees: tuple[Path, Path, str, str]
) -> None:
    _, model = _run(tmp_path, trees, honest)
    assert len(model.requests) == 2, "one call per file group, not per observation"
    prompts = [r["messages"][0]["content"] for r in model.requests]
    joined = "\n".join(p if isinstance(p, str) else json.dumps(p) for p in prompts)
    assert "    6      if percent < 0:" in joined, "excerpts carry the file's own line numbers"
    assert "DIFF of shoplib/pricing.py" in joined
    assert "Rejects a negative percent" in joined, "the author's stated intent is included"
    first, second = (r["system"] for r in model.requests)
    assert first == second, "the instructions and repository context are a shared prefix"
    assert all(block["cache_control"]["type"] == "ephemeral" for block in first)
    assert "[declared-tests]" in first[1]["text"]


def test_a_cited_answer_is_kept_with_its_suggestion(
    tmp_path: Path, trees: tuple[Path, Path, str, str]
) -> None:
    outcome, _ = _run(tmp_path, trees, honest)
    kept = {a.observation_id: a for a in outcome.adjudications}  # type: ignore[attr-defined]
    assert set(kept) == {"o-behaviour", "o-api"}
    behaviour = kept["o-behaviour"]
    assert {c.type for c in behaviour.citations} == {"code", "norm", "test"}
    assert all(c.quote for c in behaviour.citations if c.type == "code")
    assert {s.observation_id for s in outcome.suggestions} == {"o-behaviour", "o-api"}  # type: ignore[attr-defined]
    assert outcome.rejected == [] and outcome.skipped == []  # type: ignore[attr-defined]
    assert outcome.usd > 0  # type: ignore[attr-defined]


def test_fabricated_references_void_the_answer(
    tmp_path: Path, trees: tuple[Path, Path, str, str]
) -> None:
    outcome, model = _run(tmp_path, trees, fabricating)
    assert model.requests, "the model was asked"
    assert outcome.adjudications == [] and outcome.suggestions == []  # type: ignore[attr-defined]
    reasons = {why for _, why in outcome.rejected}  # type: ignore[attr-defined]
    assert reasons == {"none of its 3 citation(s) could be checked"}


def test_the_model_cannot_speak_about_what_it_was_not_shown(
    tmp_path: Path, trees: tuple[Path, Path, str, str]
) -> None:
    def invents(body: dict[str, object]) -> tuple[int, dict[str, object]]:
        return message(json.dumps({"adjudications": [{
            "observation_id": "o-invented", "assessment": "diverges_from_repo",
            "reasoning": "A new problem I found.",
            "citations": [{"type": "norm", "ref": "declared-tests"}],
        }]}))

    outcome, _ = _run(tmp_path, trees, invents)
    assert outcome.adjudications == []  # type: ignore[attr-defined]
    assert {why for _, why in outcome.rejected} == {"the answer did not address it"}  # type: ignore[attr-defined]


def test_prose_instead_of_json_is_a_gap_not_a_crash(
    tmp_path: Path, trees: tuple[Path, Path, str, str]
) -> None:
    outcome, _ = _run(tmp_path, trees, lambda body: message("This PR looks risky to me."))
    assert outcome.adjudications == []  # type: ignore[attr-defined]
    assert {why for _, why in outcome.rejected} == {"the answer was not the required JSON"}  # type: ignore[attr-defined]


def test_over_budget_nothing_is_sent_and_the_run_says_so(
    tmp_path: Path, trees: tuple[Path, Path, str, str]
) -> None:
    outcome, model = _run(tmp_path, trees, honest, BudgetConfig(per_run_usd=0.001))
    assert model.requests == []
    assert outcome.adjudications == []  # type: ignore[attr-defined]
    assert all("refused before sending" in why for _, why in outcome.skipped)  # type: ignore[attr-defined]
    assert len(outcome.skipped) == 2  # type: ignore[attr-defined]
