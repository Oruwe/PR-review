"""What a repository's reviewers have enforced, learned while the service runs.

The GitHub API is reached over real HTTP — a local server serving GitHub-shaped
payloads for the deterministic cases, and the live API against a repository in
scope when the network allows. Git repositories, the database and the clustering
are all real.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from prflagger.brain.declared import declared_norms
from prflagger.brain.keeper import BrainKeeper
from prflagger.brain.mine import agglomerate, clean_comment, lexical_vectoriser, mine_norms
from prflagger.core.config import BrainConfig, Config, RepoConfig
from prflagger.core.models import Repo
from prflagger.lang.detect import pack
from prflagger.storage.db import Database
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store
from prflagger.vcs.github import GitHub
from tests import github_fixture as gh
from tests.github_fixture import RecordedGitHub, serve

# Review comments as reviewers write them: several ways of asking for the same
# thing, and the everyday remarks that are not standards at all.
CHANGELOG = [
    ("Please add a changelog entry for this change.", "ana"),
    ("This needs an entry in CHANGES.rst for the release notes.", "ben"),
    ("Don't forget the changelog entry.", "ana"),
    ("Add a changelog entry under the unreleased section.", "cy"),
]
TYPES = [
    ("Please add type hints to this function.", "ben"),
    ("Missing type hints on the return value.", "cy"),
    ("Add type hints here too, the rest of the module has them.", "ana"),
]
NOISE = [
    ("Nit: rename this variable to something clearer.", "ben"),
    ("Why do we need this import here?", "ana"),
    ("This could be simplified with a list comprehension.", "cy"),
    ("Should this be configurable instead?", "ben"),
    ("I think this belongs in utils rather than core.", "ana"),
    ("LGTM", "cy"),
    ("> Should this be configurable?\n\nThanks!", "ana"),
]


def _comments(pairs: list[tuple[str, str]], first_pr: int = 100) -> list[dict[str, object]]:
    return [
        {"pr_number": first_pr + index, "reviewer_login": who, "body": body,
         "path": "lib/core.py",
         "html_url": f"https://github.com/acme/lib/pull/{first_pr + index}",
         "created_at": f"2026-01-{index + 1:02d}T00:00:00Z", "enforced": True}
        for index, (body, who) in enumerate(pairs)
    ]


# ----------------------------------------------------------------------------------
# Mining
# ----------------------------------------------------------------------------------


def test_recurring_standards_become_norms_and_remarks_do_not() -> None:
    corpus = _comments(CHANGELOG + TYPES + NOISE)
    norms = mine_norms(corpus, vectoriser=lexical_vectoriser())
    statements = [n.statement for n in norms]
    assert len(norms) == 2, statements
    assert any("changelog" in s.lower() for s in statements)
    assert any("type hints" in s.lower() for s in statements)
    for norm in norms:
        assert norm.source == "mined" and norm.clustered_by == "lexical"
        assert norm.named_by == "quote"
        assert norm.statement in {body for body, _ in CHANGELOG + TYPES}, (
            "without a model, a norm is stated in a reviewer's own words"
        )
        assert norm.support >= 3 and norm.distinct_reviewers >= 2
        assert norm.evidence and all(url.startswith("https://") for _, url, _ in norm.evidence)
        assert sorted(norm.evidence_prs, reverse=True) == list(norm.evidence_prs)


def test_one_reviewer_repeating_themselves_is_not_a_standard() -> None:
    lone = [(body, "ana") for body, _ in CHANGELOG]
    assert mine_norms(_comments(lone), vectoriser=lexical_vectoriser()) == []


def test_a_model_names_each_group_once_and_a_failure_falls_back_to_the_quote() -> None:
    calls: list[int] = []

    def namer(texts: object) -> str:
        calls.append(len(list(texts)))  # type: ignore[arg-type]
        return "Record every user-visible change in the changelog."

    norms = mine_norms(_comments(CHANGELOG + TYPES), vectoriser=lexical_vectoriser(),
                       namer=namer, namer_id="anthropic.claude-haiku-4-5")
    assert len(calls) == len(norms) == 2, "one naming call per group, never per comment"
    assert all(n.named_by == "anthropic.claude-haiku-4-5" for n in norms)
    assert all(n.quote for n in norms), "the reviewers' own words stay beside the summary"

    def broken(texts: object) -> str:
        raise RuntimeError("model unavailable")

    fallback = mine_norms(_comments(CHANGELOG + NOISE), vectoriser=lexical_vectoriser(),
                          namer=broken, namer_id="x")
    assert [n.named_by for n in fallback] == ["quote"]


def test_fast_clustering_matches_the_reference_average_linkage() -> None:
    """Lance-Williams updates must give what recomputing every pair gives."""

    def reference(vectors: np.ndarray, threshold: float) -> list[list[int]]:
        normalised = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        similarity = normalised @ normalised.T
        clusters = [[i] for i in range(len(vectors))]
        while len(clusters) > 1:
            best = (-1.0, -1, -1)
            for a in range(len(clusters)):
                for b in range(a + 1, len(clusters)):
                    score = float(similarity[np.ix_(clusters[a], clusters[b])].mean())
                    if score > best[0]:
                        best = (score, a, b)
            if best[0] < threshold:
                break
            _, a, b = best
            clusters[a] += clusters[b]
            del clusters[b]
        return sorted(sorted(c) for c in clusters)

    rng = np.random.default_rng(7)
    centres = rng.normal(size=(4, 12))
    vectors = np.vstack([centre + 0.35 * rng.normal(size=(9, 12)) for centre in centres])
    for threshold in (0.3, 0.6, 0.85):
        assert sorted(agglomerate(vectors, threshold)) == reference(vectors, threshold)


def test_quotes_code_and_acknowledgements_are_not_learned_from() -> None:
    assert clean_comment("LGTM") == ""
    assert clean_comment("> quoted\n\nThanks!") == ""
    assert clean_comment("```python\nx = 1\n```\nok") == ""
    assert clean_comment("@ana please add a test for the empty case") == (
        "please add a test for the empty case"
    )


# ----------------------------------------------------------------------------------
# Declared norms
# ----------------------------------------------------------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_declared_norms_cite_the_line_that_configures_them(tmp_path: Path) -> None:
    python = pack("python")
    assert python is not None
    tree = _tree(tmp_path / "py", {
        "pyproject.toml": '[project]\nname = "x"\n\n[tool.ruff]\nline-length = 100\n',
        "CHANGES.rst": "Unreleased\n",
        "tests/test_x.py": "def test_x():\n    pass\n",
    })
    norms = {n.id: n for n in declared_norms(tree, python)}
    assert set(norms) == {"declared-lint-ruff", "declared-changelog", "declared-tests"}, (
        "mypy is run by the pack but not configured by the repository, so it is not a "
        "standard the repository set"
    )
    assert norms["declared-lint-ruff"].evidence == ((0, "", "pyproject.toml:4"),)
    assert norms["declared-changelog"].evidence[0][2] == "CHANGES.rst:1"
    assert all(n.source == "declared" and n.confidence == 1.0 for n in norms.values())


def test_declared_norms_follow_the_language_pack(tmp_path: Path) -> None:
    node = pack("node")
    assert node is not None
    tree = _tree(tmp_path / "js", {
        "package.json": '{\n  "name": "x"\n}\n', "eslint.config.js": "export default [];\n",
        "tsconfig.json": "{}\n",
    })
    ids = {n.id for n in declared_norms(tree, node)}
    assert ids == {"declared-lint-eslint", "declared-lint-tsc"}


# ----------------------------------------------------------------------------------
# Keeping them current
# ----------------------------------------------------------------------------------


def _routes(pulls: list[tuple[int, str, list[tuple[str, str]]]]) -> dict[str, object]:
    """PRs newest first: (number, updated_at, [(comment, reviewer)]). Every comment
    precedes a later commit, so every one counts as enforced."""
    routes: dict[str, object] = {
        "/repos/acme/lib/pulls": [gh.pull(n, updated=at) for n, at, _ in pulls]
    }
    for number, at, comments in pulls:
        routes[f"/repos/acme/lib/pulls/{number}/comments"] = [
            gh.comment(number * 100 + i, body, user=who, at="2026-01-01T00:00:00Z")
            for i, (body, who) in enumerate(comments)
        ]
        routes[f"/repos/acme/lib/pulls/{number}/commits"] = [gh.commit(at)]
    return routes


def _keeper(tmp_path: Path, url: str, **brain: object) -> tuple[BrainKeeper, Store]:
    config = Config(repos=(RepoConfig(slug="acme/lib"), RepoConfig(slug="zeta/other")),
                    brain=BrainConfig(**brain))  # type: ignore[arg-type]
    database = Database(tmp_path / "brain.db")
    store = Store(database)
    for slug in ("acme/lib", "zeta/other"):
        store.put_repo(Repo(slug=slug, added_at=time.time()))
    keeper = BrainKeeper(config, store, EventBus(database),
                         GitHub(token="t", base_url=url))
    return keeper, store


def test_the_first_harvest_learns_and_a_quiet_day_costs_one_request(tmp_path: Path) -> None:
    pulls = [(10 + i, f"2026-02-{20 - i:02d}T00:00:00Z", [pair])
             for i, pair in enumerate(CHANGELOG + TYPES + NOISE)]
    recorded = RecordedGitHub(routes=_routes(pulls))

    async def scenario() -> None:
        keeper, store = _keeper(tmp_path, recorded.base_url)
        learned = await keeper.refresh("acme/lib", reason="first reading")
        assert learned is not None
        assert learned.pulls_read == len(pulls)
        assert learned.norms == 2
        assert store.norms("zeta/other") == [], "one repository's reviews are not another's"
        assert not keeper.due("acme/lib")

        before = len(recorded.requests)
        again = await keeper.refresh("acme/lib", force=True)
        assert again is not None and again.pulls_read == 0 and again.new_enforced == 0
        assert len(recorded.requests) - before == 1, "nothing new: one list request, no more"

    with serve(recorded):
        asyncio.run(scenario())


def test_a_new_enforced_comment_is_learned_on_the_next_refresh(tmp_path: Path) -> None:
    # Word rarity is measured across the whole history, so the first harvest carries
    # the everyday remarks every real repository has alongside its standards.
    first = TYPES + NOISE
    recorded = RecordedGitHub(routes=_routes(
        [(20 + i, f"2026-02-{20 - i:02d}T00:00:00Z", [pair]) for i, pair in enumerate(first)]
    ))

    async def scenario() -> tuple[int, int]:
        keeper, store = _keeper(tmp_path, recorded.base_url)
        await keeper.refresh("acme/lib")
        before = len(store.norms("acme/lib"))
        # Two more pull requests merge, each asking for a changelog entry.
        recorded.routes.update(_routes(
            [(40 + i, f"2026-03-{10 - i:02d}T00:00:00Z", [pair])
             for i, pair in enumerate(CHANGELOG)]
            + [(20 + i, f"2026-02-{20 - i:02d}T00:00:00Z", [pair])
               for i, pair in enumerate(first)]
        ))
        learned = await keeper.refresh("acme/lib", force=True)
        assert learned is not None and learned.new_enforced == len(CHANGELOG)
        assert learned.pulls_read == len(CHANGELOG), "only what merged since the last read"
        return before, len(store.norms("acme/lib"))

    with serve(recorded):
        counts = asyncio.run(scenario())
    assert counts == (1, 2)


def test_without_a_token_the_harvest_is_bounded_and_says_so(tmp_path: Path) -> None:
    pulls = [(i, f"2026-01-{28 - i:02d}T00:00:00Z", [("Please add a test here.", "a")])
             for i in range(1, 25)]
    recorded = RecordedGitHub(routes=_routes(pulls))

    async def scenario() -> str:
        keeper, _ = _keeper(tmp_path, recorded.base_url, unauthenticated_limit=5)
        keeper._github.token = ""  # noqa: SLF001 - the unauthenticated case
        learned = await keeper.refresh("acme/lib")
        assert learned is not None and learned.pulls_read == 5
        return learned.note

    with serve(recorded):
        note = asyncio.run(scenario())
    assert "GITHUB_TOKEN" in note


def test_a_repository_not_on_github_keeps_only_its_declared_norms(tmp_path: Path) -> None:
    config = Config(repos=(RepoConfig(slug="self/hosted", clone_url="/srv/git/x.git"),))
    database = Database(tmp_path / "b.db")
    store = Store(database)
    store.put_repo(Repo(slug="self/hosted", added_at=time.time()))
    client = GitHub(token="t", base_url="http://127.0.0.1:9")
    keeper = BrainKeeper(config, store, EventBus(database), client)
    learned = asyncio.run(keeper.refresh("self/hosted"))
    assert learned is not None and "declared norms" in learned.note
    assert client.requests_made == 0


# ----------------------------------------------------------------------------------
# Against the real API, when it can be reached
# ----------------------------------------------------------------------------------


def _reachable(slug: str) -> bool:
    # Opt-in: an anonymous request from a shared CI runner can hit GitHub's rate
    # limit, and a gating job must not go red for that. The network job sets it.
    if os.environ.get("PRFLAGGER_LIVE_GITHUB") != "1":
        return False
    try:
        client = GitHub()
        client.repo(slug)
        client.close()
    except Exception:  # noqa: BLE001 - any failure means not reachable from here
        return False
    return True


LIVE = "Oruwe/PR-review"


@pytest.mark.skipif(
    not _reachable(LIVE),
    reason=f"set PRFLAGGER_LIVE_GITHUB=1 with api.github.com/{LIVE} reachable",
)
def test_a_live_harvest_reads_merged_pull_requests_incrementally() -> None:
    from prflagger.brain.harvest import fetch_reviews

    client = GitHub()
    records = fetch_reviews(client, LIVE, limit=10)
    assert records, f"{LIVE} has merged pull requests"
    assert all(r["merged_at"] for r in records)
    spent = client.requests_made
    assert spent == 1 + 2 * len(records), "a list, then comments and commits per merged PR"
    assert fetch_reviews(client, LIVE, since=records[0]["updated_at"]) == []
    assert client.requests_made == spent + 1, "nothing new costs a single request"


def test_the_git_fixture_helpers_are_real(tmp_path: Path) -> None:
    """Guard: `_tree` builds real files; `declared_norms` reads them from disk."""
    tree = _tree(tmp_path / "g", {"tests/a.py": "x = 1\n"})
    subprocess.run(["git", "init", "-q", str(tree)], check=True)  # noqa: S603, S607
    python = pack("python")
    assert python is not None
    assert [n.id for n in declared_norms(tree, python)] == ["declared-tests"]
