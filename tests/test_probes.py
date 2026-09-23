"""C9 acceptance.

SPEC.md § C9: on `seed/untested-api`, `coverage_delta` returns a Finding for the new
function and `api_diff` reports one added public symbol.

No LLM is involved in any of this — coverage.py, the repo's own ruff and mypy, and an
`ast` diff. Everything runs against the real target repo in the real sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prflagger.models import Finding
from prflagger.probes import api_diff, coverage_delta, lint_regression
from prflagger.probes._support import SEVERITY
from prflagger.probes.api_diff import public_surface
from tests.requires import require_git

SEEDS = Path(".cache/seeds.json")


@pytest.fixture
def seeds() -> dict[str, dict[str, str]]:
    if not SEEDS.is_file():
        require_git()  # seeding needs git; without it these cannot run at all
        pytest.fail("run `python -m scripts.seed_target` to create the seeded branches")
    return json.loads(SEEDS.read_text(encoding="utf-8"))


def _contract_holds(finding: Finding) -> None:
    """Every emitted finding carries all four fields."""
    assert finding.what_changed
    assert finding.how_we_know
    assert finding.norm is not None, "only behavior_change may omit a norm"
    assert 0.0 <= finding.confidence <= 1.0


# --------------------------------------------------------------------------------------
# api_diff
# --------------------------------------------------------------------------------------


def test_api_diff_reports_one_added_public_symbol(
    seeds: dict[str, dict[str, str]], head_worktree: Path
) -> None:
    meta = seeds["seed/untested-api"]
    findings = api_diff(head_worktree, meta["base"], meta["head"])

    assert len(findings) == 1, [f.symbol for f in findings]
    finding = findings[0]
    assert finding.symbol == "click.shell_completion.join_arg_string"
    assert finding.kind == "api_change"
    assert "added to the public API" in finding.what_changed
    assert finding.severity == SEVERITY["api_change"] == 0.8
    _contract_holds(finding)


def test_api_diff_is_silent_when_the_surface_does_not_move(
    seeds: dict[str, dict[str, str]], head_worktree: Path
) -> None:
    # Both of these change behaviour inside existing functions, not the API surface.
    for branch in ("seed/honest-fix", "seed/silent-edge"):
        meta = seeds[branch]
        assert api_diff(head_worktree, meta["base"], meta["head"]) == []


def test_public_surface_excludes_private_paths(head_worktree: Path, seeds: dict) -> None:
    surface = public_surface(head_worktree, seeds["seed/honest-fix"]["base"])

    assert surface, "the target repo has a public API"
    assert all(not part.startswith("_") for fqn in surface for part in fqn.split("."))
    assert "click.shell_completion.split_arg_string" in surface
    # Private behaviour is allowed to change; flagging it is noise.
    assert not any(fqn.startswith("click._compat") for fqn in surface)


def test_signature_records_what_callers_may_omit(head_worktree: Path, seeds: dict) -> None:
    surface = public_surface(head_worktree, seeds["seed/honest-fix"]["base"])
    signature = surface["click.shell_completion.split_arg_string"]
    assert signature.startswith("(string)")
    assert "defaults=" in signature


# --------------------------------------------------------------------------------------
# coverage_delta
# --------------------------------------------------------------------------------------


def test_coverage_delta_finds_the_untested_new_function(
    seeds: dict[str, dict[str, str]], head_worktree: Path
) -> None:
    meta = seeds["seed/untested-api"]
    findings = coverage_delta(head_worktree, meta["base"], meta["head"])

    assert len(findings) == 1, [f.symbol for f in findings]
    finding = findings[0]
    assert finding.symbol == "click.shell_completion.join_arg_string"
    assert finding.kind == "coverage_gap"
    assert finding.severity == SEVERITY["coverage_gap"] == 0.5
    # The evidence is a measurement, with the range it was measured over.
    assert "coverage.py" in finding.how_we_know
    assert "0 of" in finding.how_we_know
    assert "src/click/shell_completion.py" in finding.how_we_know
    _contract_holds(finding)


def test_coverage_delta_is_silent_when_changed_code_is_exercised(
    seeds: dict[str, dict[str, str]], head_worktree: Path
) -> None:
    # split_arg_string is covered by the repo's own suite, so changing it is no gap.
    meta = seeds["seed/honest-fix"]
    assert coverage_delta(head_worktree, meta["base"], meta["head"]) == []


# --------------------------------------------------------------------------------------
# lint_regression
# --------------------------------------------------------------------------------------


def test_lint_regression_reports_new_errors_not_pre_existing_ones(
    seeds: dict[str, dict[str, str]], head_worktree: Path
) -> None:
    """The target repo has dozens of pre-existing mypy errors. None of them are this
    PR's, so none may be reported."""
    from prflagger.characterize.validate import base_checkout
    from prflagger.probes.lint import lint_errors

    meta = seeds["seed/untested-api"]
    at_head = lint_errors(base_checkout(head_worktree, meta["head"]), meta["head"])
    assert at_head is not None
    assert len(at_head["mypy"]) > 0, "the target repo does have pre-existing errors"

    findings = lint_regression(head_worktree, meta["base"], meta["head"])
    assert findings == [], "pre-existing errors are not a regression"


def test_lint_errors_parse_with_their_codes(
    seeds: dict[str, dict[str, str]], head_worktree: Path
) -> None:
    from prflagger.characterize.validate import base_checkout
    from prflagger.probes.lint import lint_errors

    meta = seeds["seed/untested-api"]
    errors = lint_errors(base_checkout(head_worktree, meta["head"]), meta["head"])
    assert errors is not None

    # mypy's pretty output wraps long messages; a truncated fragment is not evidence.
    coded = [error for error in errors["mypy"] if error[1]]
    assert coded, "mypy errors must carry their error code"
    assert any(message.endswith(('"', ")", ".", "s")) for _, _, message in coded)
    assert all(not path.startswith("/src") for path, _, _ in errors["mypy"])


# --------------------------------------------------------------------------------------
# Severity table
# --------------------------------------------------------------------------------------


def test_severity_matches_the_spec_table() -> None:
    assert SEVERITY == {"api_change": 0.8, "coverage_gap": 0.5, "lint_regression": 0.3}
