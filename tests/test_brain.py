"""C5 and C6 acceptance.

SPEC.md § C5: after one run, prs.json contains >= 100 PRs and >= 1 with a non-empty
review comment list. A second `harvest` call makes zero network requests (assert with a
counter around the subprocess call).

SPEC.md § C6: output is a strict subset of all review comments; every kept comment has
at least one later commit on its PR; comments on unmerged PRs are absent. Log the
retention rate.

The live fetch cannot run here: `gh` is not installed and this session has no GitHub API
access to the target repo, so the ">= 100 PRs" half of C5 is UNMET, not relaxed. The
cache-first half is tested for real. C6 is a pure filter and is tested against a corpus
in GitHub's own response shape.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from prflagger.brain import harvest as harvest_module
from prflagger.brain.enforce import enforced_comments, retention_rate
from prflagger.brain.harvest import GhUnavailable, harvest, prs_path

CORPUS = Path(__file__).resolve().parent / "fixtures" / "prs_corpus.json"


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------------------
# C5 — never re-fetch what is cached
# --------------------------------------------------------------------------------------


def test_a_cached_harvest_makes_zero_network_requests(
    isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def counting(argv: list[str]):  # type: ignore[no-untyped-def]
        calls.append(argv)
        raise AssertionError("harvest must not reach the network when cached")

    monkeypatch.setattr(harvest_module, "_gh", counting)

    path = prs_path("pallets/click")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(CORPUS, path)

    returned = harvest("pallets/click")

    assert returned == path
    assert calls == [], "a second harvest must be served from disk"
    assert json.loads(returned.read_text(encoding="utf-8"))


def test_harvest_says_why_when_gh_is_missing(
    isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harvest_module.shutil, "which", lambda _: None)
    with pytest.raises(GhUnavailable, match="gh CLI"):
        harvest("pallets/click")


# --------------------------------------------------------------------------------------
# C6 — the enforcement filter
# --------------------------------------------------------------------------------------


def _all_comments() -> list[dict[str, object]]:
    records = json.loads(CORPUS.read_text(encoding="utf-8"))
    return [comment for pull in records for comment in pull["review_comments"]]


def test_output_is_a_strict_subset_of_all_review_comments() -> None:
    kept = enforced_comments(CORPUS)
    everything = _all_comments()

    assert 0 < len(kept) < len(everything), "the filter must actually remove something"
    bodies = {str(comment["body"]) for comment in everything}
    assert all(comment["body"] in bodies for comment in kept)


def test_every_kept_comment_has_a_later_commit_on_its_pr() -> None:
    from datetime import datetime

    records = {pull["number"]: pull for pull in json.loads(CORPUS.read_text(encoding="utf-8"))}
    for comment in enforced_comments(CORPUS):
        pull = records[comment["pr_number"]]
        latest = max(
            datetime.fromisoformat(
                (c["commit"].get("committer") or c["commit"]["author"])["date"].replace(
                    "Z", "+00:00"
                )
            )
            for c in pull["commits"]
        )
        created = datetime.fromisoformat(str(comment["created_at"]).replace("Z", "+00:00"))
        assert created < latest, f"{comment['body']!r} had nothing land after it"


def test_comments_on_unmerged_prs_are_absent() -> None:
    kept = enforced_comments(CORPUS)
    assert all(comment["pr_number"] != 500 for comment in kept)
    # PR 500's comment text is identical to an enforced one, so only the chain can
    # distinguish them — which is the whole point of the filter.
    assert any(c["body"] == "This needs a test in the same PR." for c in kept)


def test_a_comment_with_no_commit_after_it_is_dropped() -> None:
    # PR 501 merged, but the only commit predates the review: nothing was enforced.
    assert all(comment["pr_number"] != 501 for comment in enforced_comments(CORPUS))


def test_a_comment_after_the_last_commit_is_dropped() -> None:
    # The nit on PR 412 arrived after the final commit, so the author never acted on it.
    kept = enforced_comments(CORPUS)
    assert all("named this differently" not in str(c["body"]) for c in kept)


def test_kept_comments_carry_the_five_documented_fields() -> None:
    for comment in enforced_comments(CORPUS):
        assert set(comment) == {
            "pr_number",
            "reviewer_login",
            "body",
            "diff_hunk",
            "created_at",
        }
        assert comment["reviewer_login"]
        assert comment["body"]


def test_retention_rate_is_reported() -> None:
    kept = enforced_comments(CORPUS)
    rate = retention_rate(len(_all_comments()), len(kept))
    assert 0.0 < rate < 1.0
    assert rate == pytest.approx(len(kept) / len(_all_comments()))


def test_a_missing_corpus_is_empty_rather_than_an_exception(tmp_path: Path) -> None:
    assert enforced_comments(tmp_path / "nope.json") == []
