"""Internals that the acceptance blocks reach only indirectly.

Each of these is a place where a quiet bug would not fail a test but would corrupt a
finding: a cache key that ignores a parameter, a worktree that gets re-created, a
report parser that silently returns nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from prflagger import llm
from prflagger.characterize import differential as differential_module
from prflagger.llm import cache as llm_cache
from prflagger.models import Finding, Norm
from prflagger.probes._support import norm_for
from prflagger.report.rank import rank
from prflagger.report.render import _coverage, citation
from prflagger.sandbox import runner

# --------------------------------------------------------------------------------------
# llm
# --------------------------------------------------------------------------------------


def test_embed_reports_unavailability_rather_than_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    if llm.embeddings_available():
        vectors = llm.embed(["hello", "world"])
        assert len(vectors) == 2 and vectors[0]
    else:
        with pytest.raises(llm.EmbeddingUnavailable):
            llm.embed(["hello"])


def test_embed_serves_cached_vectors_without_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached vector must come back even when the model cannot be loaded, which is
    what makes a run reproducible after the first."""
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))

    class FakeModel:
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.5, 0.25] for _ in texts]

    monkeypatch.setattr(llm_cache, "_embed_model", lambda: FakeModel())
    first = llm.embed(["a norm statement"])
    assert first == [[0.5, 0.25]]

    def unavailable() -> object:
        raise llm.EmbeddingUnavailable("model gone")

    monkeypatch.setattr(llm_cache, "_embed_model", unavailable)
    assert llm.embed(["a norm statement"]) == [[0.5, 0.25]]


def test_a_partially_cached_batch_only_encodes_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    encoded: list[list[str]] = []

    class FakeModel:
        def encode(self, texts: list[str]) -> list[list[float]]:
            encoded.append(list(texts))
            return [[float(len(text))] for text in texts]

    monkeypatch.setattr(llm_cache, "_embed_model", lambda: FakeModel())
    llm.embed(["one"])
    llm.embed(["one", "two"])

    assert encoded == [["one"], ["two"]], "a cached text must not be re-encoded"


def test_the_usage_ledger_records_only_real_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    calls = {"n": 0}

    def provider(prompt: str, max_tokens: int, temperature: float) -> str:
        calls["n"] += 1
        return "answer"

    for _ in range(3):
        llm.complete("same prompt", model="m", provider=provider)

    assert calls["n"] == 1
    records = [
        json.loads(line)
        for line in llm.usage_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["token_source"] == "estimate"
    assert records[0]["completion_chars"] == len("answer")


def test_a_corrupt_llm_cache_entry_is_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    calls = {"n": 0}

    def provider(prompt: str, max_tokens: int, temperature: float) -> str:
        calls["n"] += 1
        return "fresh"

    llm.complete("p", model="m", provider=provider)
    for entry in llm.cache_dir().glob("*.json"):
        entry.write_text("{not json", encoding="utf-8")

    assert llm.complete("p", model="m", provider=provider) == "fresh"
    assert calls["n"] == 2, "a corrupt entry must be re-fetched, not returned as text"


# --------------------------------------------------------------------------------------
# runner internals
# --------------------------------------------------------------------------------------


def test_the_image_key_changes_with_the_lockfile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname="a"\n', encoding="utf-8")
    first = runner.lockfile_image_key(repo)

    assert runner.lockfile_image_key(repo) == first, "stable for equal content"
    (repo / "pyproject.toml").write_text('[project]\nname="b"\n', encoding="utf-8")
    assert runner.lockfile_image_key(repo) != first

    (repo / "uv.lock").write_text("x", encoding="utf-8")
    assert runner.lockfile_image_key(repo) != first


def test_a_worktree_is_reused_and_never_recloned(target: dict[str, str], head_sha: str) -> None:
    slug = str(target["slug"])
    first = runner.worktree_for(slug, head_sha)
    marker = first / ".prflagger_reuse_marker"
    marker.write_text("x", encoding="utf-8")
    try:
        second = runner.worktree_for(slug, head_sha)
        assert second == first
        assert marker.is_file(), "an existing worktree must be handed back untouched"
    finally:
        marker.unlink(missing_ok=True)


def test_the_bare_clone_is_reused(target: dict[str, str]) -> None:
    slug = str(target["slug"])
    path = runner.bare_clone(slug)
    assert path.is_dir()
    assert runner.bare_clone(slug) == path
    # It is bare: a working tree would mean we cloned the wrong way.
    assert not (path / "src").exists()


def test_extracting_a_json_report_out_of_noisy_stdout() -> None:
    report = {"tests": [{"nodeid": "t.py::test_a", "outcome": "passed"}]}
    stdout = "collecting ...\n{'not': 'json'}\n" + json.dumps(report) + "\n1 passed\n"

    extracted = runner._extract_report(stdout)
    assert extracted is not None
    assert runner._per_test(extracted) == {"t.py::test_a": "passed"}


def test_no_report_means_no_per_test_rather_than_a_crash() -> None:
    assert runner._extract_report("1 passed in 0.1s") is None
    assert runner._per_test(None) == {}
    assert runner._per_test({"tests": "not a list"}) == {}


def test_the_mounted_checkout_takes_precedence_on_the_path(head_worktree: Path) -> None:
    """Without this the tests import the build-time copy and base and head are
    indistinguishable."""
    pythonpath = runner._pythonpath_for(head_worktree)
    assert pythonpath.startswith("/src/")
    assert pythonpath.split(":")[0] == "/src/src"


# --------------------------------------------------------------------------------------
# differential / probes / report helpers
# --------------------------------------------------------------------------------------


def test_failure_blocks_split_per_test() -> None:
    output = (
        "=================================== FAILURES ==========\n"
        "_______ test_char_0 _______\n"
        "    assert 1 == 2\nE   assert False\n"
        "_______ test_char_3 _______\n"
        "    assert x == []\nE   ValueError\n"
        "=========================== short test summary info ====\n"
        "FAILED test_char_0\n"
    )
    blocks = differential_module._failure_blocks(output)

    assert set(blocks) == {"test_char_0", "test_char_3"}
    assert "assert 1 == 2" in blocks["test_char_0"]
    assert "ValueError" in blocks["test_char_3"]
    assert "short test summary" not in blocks["test_char_3"]


def test_no_failures_means_no_blocks() -> None:
    assert differential_module._failure_blocks("2 passed in 0.1s") == {}


def test_a_probe_without_a_declared_norm_emits_nothing(tmp_path: Path) -> None:
    # A finding without a norm is not a finding, so a repo declaring nothing gets none.
    assert norm_for("coverage_gap", tmp_path) is None
    assert norm_for("not_a_kind", tmp_path) is None


def test_norm_for_finds_the_repos_declared_standard(head_worktree: Path) -> None:
    norm = norm_for("coverage_gap", head_worktree)
    assert norm is not None
    assert norm.id == "declared-tests-exist"


def test_citation_reads_as_the_maintainers_own_words() -> None:
    assert citation((412, 457, 490)) == "This repo required this in #412, #457, #490."
    assert citation((7,)) == "This repo required this in #7."


def test_a_profile_without_a_coverage_block_is_empty_not_invented() -> None:
    assert _coverage({}) == {"verified": [], "skipped": []}


def test_rank_uses_a_better_matching_norm_when_the_store_offers_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prflagger.brain.store import SqliteGraphStore

    mined = Norm(
        id="mined", statement="Add a test with every behavioural change.", scope="repo",
        support=9, distinct_reviewers=4, confidence=0.9, evidence_prs=(11, 22),
    )
    declared = Norm(
        id="declared-tests-exist", statement="New code must be exercised.", scope="repo",
        support=0, distinct_reviewers=0, confidence=1.0, evidence_prs=(),
    )
    finding = Finding(
        kind="coverage_gap", symbol="pkg.a", what_changed="uncovered",
        how_we_know="coverage.py", norm=declared, confidence=1.0, severity=0.5,
    )

    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    monkeypatch.setattr(
        SqliteGraphStore, "match_norm", lambda self, repo, text, k=3: [(mined, 0.81)]
    )
    ranked = rank([finding], [], store)

    assert ranked[0].norm is not None
    assert ranked[0].norm.id == "mined", "a confident match replaces the declared norm"
    assert ranked[0].norm.evidence_prs == (11, 22)


def test_a_weak_match_leaves_the_declared_norm_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from prflagger.brain.store import SqliteGraphStore

    weak = Norm(
        id="unrelated", statement="Unrelated.", scope="repo", support=3,
        distinct_reviewers=2, confidence=0.5, evidence_prs=(1,),
    )
    declared = Norm(
        id="declared-tests-exist", statement="New code must be exercised.", scope="repo",
        support=0, distinct_reviewers=0, confidence=1.0, evidence_prs=(),
    )
    finding = Finding(
        kind="coverage_gap", symbol="pkg.a", what_changed="uncovered",
        how_we_know="coverage.py", norm=declared, confidence=1.0, severity=0.5,
    )

    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    monkeypatch.setattr(
        SqliteGraphStore, "match_norm", lambda self, repo, text, k=3: [(weak, 0.41)]
    )
    # Below 0.6 the match is not good enough to cite, and a citation nobody can check
    # is worse than none.
    assert rank([finding], [], store)[0].norm is declared


# --------------------------------------------------------------------------------------
# The seeding script is reproducible
# --------------------------------------------------------------------------------------


def test_reseeding_is_idempotent() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    before = json.loads((repo_root / ".cache" / "seeds.json").read_text(encoding="utf-8"))

    completed = subprocess.run(
        ["python3", "-m", "scripts.seed_target"],
        cwd=repo_root, capture_output=True, text=True, timeout=600,
    )
    assert completed.returncode == 0, completed.stderr[-1500:]

    after = json.loads((repo_root / ".cache" / "seeds.json").read_text(encoding="utf-8"))
    assert {b: m["head"] for b, m in after.items()} == {
        b: m["head"] for b, m in before.items()
    }, "re-running the seeder must land on the same commits"


# --------------------------------------------------------------------------------------
# A CA path this machine cannot read is "no CA," not a crash
# --------------------------------------------------------------------------------------


def test_readable_file_treats_permission_denied_as_absent(monkeypatch, tmp_path) -> None:
    # The real bug: on a machine where the proxy CA path exists but is owned by
    # someone else (a normal CI runner, not this sandbox), Path.is_file() itself
    # raises PermissionError rather than returning False.
    target = tmp_path / "ca-bundle.crt"
    target.write_text("not real")

    def _raise(self: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "is_file", _raise)
    assert runner._readable_file(target) is False


def test_readable_file_still_finds_a_real_one(tmp_path) -> None:
    target = tmp_path / "ca-bundle.crt"
    target.write_text("not real")
    assert runner._readable_file(target) is True


def test_stage_ca_bundle_degrades_to_none_on_permission_denied(monkeypatch) -> None:
    # _CA_FALLBACK is this one sandbox's proxy cert. On any other machine it must
    # never crash the build — it must mean "this build has no CA to stage."
    monkeypatch.delenv("PRFLAGGER_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    def _raise(self: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "is_file", _raise)
    assert runner._stage_ca_bundle() is None
