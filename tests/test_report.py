"""C10 acceptance.

SPEC.md § C10: `rank` is deterministic — same input, same order, twice. `render`
produces an HTML file that opens standalone with no network requests, containing all
three seeded PRs' findings.

The findings are real: probe findings come straight from the probes, and the behaviour
changes come from `differential` with only the text generator stubbed, as in C4.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from prflagger.brain.store import SqliteGraphStore
from prflagger.characterize import differential as differential_module
from prflagger.characterize import generate
from prflagger.characterize import validate as validate_module
from prflagger.characterize.differential import differential
from prflagger.models import Finding, Norm
from prflagger.probes import api_diff, coverage_delta
from prflagger.report.rank import MATCH_THRESHOLD, SCOPE_WEIGHT, centrality, rank
from prflagger.report.render import render
from tests.test_differential import GENERATED, KNOWN_SYMBOLS

SEEDS = Path(".cache/seeds.json")

PROFILE = {
    "repo": "pallets/click",
    "generated_at": "2026-09-17",
    "prs_analyzed": 0,
    "declared": {"linter": "ruff", "type_checker": "mypy --strict"},
    "coverage": {
        "verified": [
            {"module": "click/shell_completion.py", "detail": "2 characterization tests"}
        ],
        "skipped": [{"module": "click/_winconsole.py", "reason": "no importable harness"}],
    },
}

CITED_NORM = Norm(
    id="test-for-new-public-fn",
    statement="New public functions require a test in the same pull request.",
    scope="repo",
    support=23,
    distinct_reviewers=4,
    confidence=1.0,
    evidence_prs=(412, 457, 490),
)


@pytest.fixture(scope="session")
def all_seed_findings(head_worktree: Path) -> list[Finding]:
    """Findings from all three seeded branches."""
    if not SEEDS.is_file():
        pytest.fail("run `python -m scripts.seed_target` first")
    seeds = json.loads(SEEDS.read_text(encoding="utf-8"))

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        generate,
        "complete",
        lambda prompt, **kw: next(
            (tests for fqn, tests in GENERATED.items() if fqn in prompt), ""
        ),
    )
    monkey.setattr(
        differential_module,
        "complete",
        lambda prompt, **kw: "\n".join(n for n in KNOWN_SYMBOLS if n in prompt),
    )
    monkey.setattr(
        validate_module,
        "complete",
        lambda prompt, **kw: (_ for _ in ()).throw(AssertionError("must not regenerate")),
    )
    try:
        findings: list[Finding] = []
        for branch in ("seed/honest-fix", "seed/silent-edge"):
            meta = seeds[branch]
            findings.extend(differential(head_worktree, meta["base"], meta["head"]))
        meta = seeds["seed/untested-api"]
        findings.extend(api_diff(head_worktree, meta["base"], meta["head"]))
        findings.extend(coverage_delta(head_worktree, meta["base"], meta["head"]))
        return findings
    finally:
        monkey.undo()


@pytest.fixture
def store(tmp_path: Path) -> SqliteGraphStore:
    return SqliteGraphStore(tmp_path / "brain.sqlite")


# --------------------------------------------------------------------------------------
# rank is deterministic
# --------------------------------------------------------------------------------------


def test_rank_is_deterministic(
    all_seed_findings: list[Finding], store: SqliteGraphStore
) -> None:
    first = rank(list(all_seed_findings), [], store)
    second = rank(list(all_seed_findings), [], store)

    assert [(f.symbol, f.kind, f.how_we_know) for f in first] == [
        (f.symbol, f.kind, f.how_we_know) for f in second
    ]
    assert len(first) == len(all_seed_findings), "ranking never drops a finding"


def test_rank_orders_by_the_published_formula(store: SqliteGraphStore) -> None:
    low = Finding(
        kind="lint_regression", symbol="pkg.a", what_changed="w", how_we_know="h",
        norm=CITED_NORM, confidence=1.0, severity=0.3,
    )
    high = Finding(
        kind="behavior_change", symbol="pkg.b", what_changed="w", how_we_know="h",
        norm=None, confidence=0.9, severity=1.0,
    )
    ordered = rank([low, high], [], store)
    # 0.9 * 1.0 beats 1.0 * 0.3 with centrality and scope equal.
    assert [f.symbol for f in ordered] == ["pkg.b", "pkg.a"]


def test_centrality_never_zeroes_a_brand_new_symbol() -> None:
    # A new public function has no callers yet; a bare ratio would sink exactly the
    # finding worth reading.
    assert centrality(0, 40) > 0.0
    assert centrality(40, 40) > centrality(0, 40)
    assert centrality(0, 0) == 1.0


def test_scope_weights_match_the_spec() -> None:
    assert SCOPE_WEIGHT == {"repo": 1.0, "project": 0.8, "org": 0.6}
    assert MATCH_THRESHOLD == 0.6


def test_rank_contains_no_llm_call(
    monkeypatch: pytest.MonkeyPatch, store: SqliteGraphStore
) -> None:
    import prflagger.llm as llm_module

    def explode(*args: object, **kwargs: object) -> str:
        raise AssertionError("rank must not call a model")

    monkeypatch.setattr(llm_module, "complete", explode)
    finding = Finding(
        kind="api_change", symbol="pkg.a", what_changed="w", how_we_know="h",
        norm=CITED_NORM, confidence=1.0, severity=0.8,
    )
    assert rank([finding], [], store) == [finding]


# --------------------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------------------


def test_report_contains_all_three_seeds_and_opens_standalone(
    all_seed_findings: list[Finding], store: SqliteGraphStore, tmp_path: Path
) -> None:
    ordered = rank(list(all_seed_findings), [], store)
    out = tmp_path / "report.html"
    render(ordered, PROFILE, out)

    html = out.read_text(encoding="utf-8")

    # All three seeded branches are represented.
    assert "click.formatting.measure_table" in html          # seed/silent-edge
    assert "click.shell_completion.split_arg_string" in html  # seed/honest-fix
    assert "click.shell_completion.join_arg_string" in html   # seed/untested-api

    # Standalone: nothing is fetched when this file is opened.
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html, re.IGNORECASE)
    assert "<style>" in html, "styling must be inline"


def test_every_finding_shows_all_four_contract_fields(
    all_seed_findings: list[Finding], tmp_path: Path
) -> None:
    out = tmp_path / "report.html"
    render(list(all_seed_findings), PROFILE, out)
    html = out.read_text(encoding="utf-8")

    assert html.count("What changed") == len(all_seed_findings)
    assert html.count("How we know") == len(all_seed_findings)
    assert html.count("Repo standard") == len(all_seed_findings)
    assert html.count("confidence") >= len(all_seed_findings)


def test_norm_citations_render_as_pr_numbers(tmp_path: Path) -> None:
    finding = Finding(
        kind="coverage_gap", symbol="pkg.retry_with_backoff",
        what_changed="Missing test for new public function retry_with_backoff",
        how_we_know="coverage.py: 0 of 7 body lines executed",
        norm=CITED_NORM, confidence=1.0, severity=0.5,
    )
    out = tmp_path / "report.html"
    render([finding], PROFILE, out)

    html = out.read_text(encoding="utf-8")
    assert "This repo required this in #412, #457, #490." in html


def test_a_declarative_norm_says_so_instead_of_citing_prs(tmp_path: Path) -> None:
    declared = Norm(
        id="declared-ruff-clean", statement="Code must pass the repository's own ruff config.",
        scope="repo", support=0, distinct_reviewers=0, confidence=1.0, evidence_prs=(),
    )
    finding = Finding(
        kind="lint_regression", symbol="src/pkg/a.py", what_changed="new F841",
        how_we_know="ruff at head", norm=declared, confidence=1.0, severity=0.3,
    )
    out = tmp_path / "report.html"
    render([finding], PROFILE, out)
    html = out.read_text(encoding="utf-8")
    assert "Declared by the repository" in html
    assert "required this in" not in html


def test_the_coverage_statement_names_what_was_skipped(tmp_path: Path) -> None:
    out = tmp_path / "report.html"
    render([], PROFILE, out)
    html = out.read_text(encoding="utf-8")

    assert "Coverage statement" in html
    assert "click/_winconsole.py" in html
    assert "no importable harness" in html
    assert "Verified 1 of 2 changed modules" in html
    # An empty report must not read as an approval.
    assert "not an approval" in html


def test_an_invalid_finding_raises_rather_than_being_skipped(tmp_path: Path) -> None:
    good = Finding(
        kind="api_change", symbol="pkg.a", what_changed="w", how_we_know="h",
        norm=CITED_NORM, confidence=1.0, severity=0.8,
    )
    # Frozen dataclasses validate in __post_init__, so an invalid one can only arrive
    # by bypassing it. The renderer is the last gate before a reader sees it.
    broken = Finding(
        kind="api_change", symbol="pkg.b", what_changed="w", how_we_know="h",
        norm=CITED_NORM, confidence=1.0, severity=0.8,
    )
    object.__setattr__(broken, "norm", None)

    out = tmp_path / "report.html"
    with pytest.raises(ValueError, match="no norm"):
        render([good, broken], PROFILE, out)


def test_render_escapes_content_rather_than_injecting_it(tmp_path: Path) -> None:
    finding = Finding(
        kind="behavior_change", symbol="pkg.a",
        what_changed="<script>alert(1)</script>", how_we_know="h",
        norm=None, confidence=0.9, severity=1.0,
    )
    out = tmp_path / "report.html"
    render([finding], PROFILE, out)
    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
