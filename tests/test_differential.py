"""C4 acceptance.

SPEC.md § C4: on seeded PR #2 (`seed/silent-edge`), `differential` returns >= 1 Finding
whose symbol is the one the seeded bug touches. On seeded PR #1 (`seed/honest-fix`), it
returns zero undeclared findings.

The seeded branches are real commits on the real target repo, and every test runs in the
real sandbox. Two stubs sit at the provider boundary, because Bedrock is not reachable
from this environment:

  * the generator returns characterization tests keyed only off the symbol name;
  * the scope extractor reports which known symbols the PR body actually names.

Neither is tuned per seed — both branches carry the same PR body and get the same
declared scope. Every difference between the two results is therefore produced by
executing the real diff, not by the stubs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prflagger.characterize import differential as differential_module
from prflagger.characterize import generate
from prflagger.characterize import validate as validate_module
from prflagger.characterize.differential import declared_scope, differential
from tests.requires import require_git

SEEDS = Path(".cache/seeds.json")

# Recorded behaviour of the unchanged code, verified directly in the sandbox:
#   split_arg_string("a\x00b") -> ["a\x00b"]   measure_table([]) -> ()
#   split_arg_string("a b") -> ["a","b"]       measure_table([("ab","c")]) -> (2, 1)
GENERATED: dict[str, str] = {
    "click.shell_completion.split_arg_string": '''
from click.shell_completion import split_arg_string


def test_split_arg_string_null_byte():
    assert split_arg_string("a\\x00b") == ["a\\x00b"]


def test_split_arg_string_simple():
    assert split_arg_string("a b") == ["a", "b"]
''',
    "click.formatting.measure_table": '''
from click.formatting import measure_table


def test_measure_table_empty():
    assert measure_table([]) == ()


def test_measure_table_one_row():
    assert measure_table([("ab", "c")]) == (2, 1)
''',
}

KNOWN_SYMBOLS = ("split_arg_string", "measure_table", "join_arg_string")


@pytest.fixture
def seeds() -> dict[str, dict[str, str]]:
    if not SEEDS.is_file():
        require_git()  # seeding needs git; without it these cannot run at all
        pytest.fail("run `python -m scripts.seed_target` to create the seeded branches")
    return json.loads(SEEDS.read_text(encoding="utf-8"))


def _must_not_regenerate(prompt: str, **kwargs: object) -> str:
    raise AssertionError(f"a recorded behaviour failed on base:\n{prompt[-1500:]}")


@pytest.fixture
def stubbed_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both stubs are blind to which seed is running."""

    def generator(prompt: str, **kwargs: object) -> str:
        for fqn, tests in GENERATED.items():
            if fqn in prompt:
                return tests
        return ""

    def extractor(prompt: str, **kwargs: object) -> str:
        # What the PR text actually names — the same rule for every branch.
        return "\n".join(name for name in KNOWN_SYMBOLS if name in prompt)

    monkeypatch.setattr(generate, "complete", generator)
    # Regeneration is the same generator boundary; a real call here would mean a
    # characterization test failed on base, which these recorded behaviours must not.
    monkeypatch.setattr(validate_module, "complete", _must_not_regenerate)
    monkeypatch.setattr(differential_module, "complete", extractor)


@pytest.fixture
def base_tree(head_worktree: Path) -> Path:
    return head_worktree


# --------------------------------------------------------------------------------------
# Seeded PR #2 — the silent edge case
# --------------------------------------------------------------------------------------


def test_silent_edge_is_found_on_the_symbol_the_bug_touches(
    seeds: dict[str, dict[str, str]], base_tree: Path, stubbed_llm: None
) -> None:
    meta = seeds["seed/silent-edge"]
    findings = differential(base_tree, meta["base"], meta["head"])

    assert findings, "the silent edge case must surface"
    on_measure_table = [f for f in findings if f.symbol == "click.formatting.measure_table"]
    assert on_measure_table, f"expected measure_table, got {[f.symbol for f in findings]}"

    finding = on_measure_table[0]
    assert finding.kind == "behavior_change"
    assert finding.norm is None  # self-justifying; C10 may attach one
    assert finding.severity == 1.0
    assert "test_char_" in finding.how_we_know
    # The PR body never mentions measure_table, so this is undeclared and not discounted.
    assert "declared" not in finding.what_changed
    assert finding.confidence == pytest.approx(0.9)


def test_the_declared_change_is_marked_and_discounted(
    seeds: dict[str, dict[str, str]], base_tree: Path, stubbed_llm: None
) -> None:
    meta = seeds["seed/silent-edge"]
    findings = differential(base_tree, meta["base"], meta["head"])

    declared = [f for f in findings if f.symbol.endswith("split_arg_string")]
    assert declared, "the stated TypeError change is a real behaviour change too"
    assert "declared in the PR description" in declared[0].what_changed
    assert declared[0].confidence == pytest.approx(0.45)  # 0.9 halved


# --------------------------------------------------------------------------------------
# Seeded PR #1 — the honest fix
# --------------------------------------------------------------------------------------


def test_honest_fix_returns_zero_undeclared_findings(
    seeds: dict[str, dict[str, str]], base_tree: Path, stubbed_llm: None
) -> None:
    meta = seeds["seed/honest-fix"]
    findings = differential(base_tree, meta["base"], meta["head"])

    undeclared = [
        f for f in findings if "declared in the PR description" not in f.what_changed
    ]
    assert undeclared == [], f"false alarms on an honest fix: {[f.symbol for f in undeclared]}"


def test_honest_fix_still_observes_the_declared_change(
    seeds: dict[str, dict[str, str]], base_tree: Path, stubbed_llm: None
) -> None:
    # The tool is not silent on an honest PR — it observes the change and says the
    # author declared it. That is the difference between a verifier and a nag.
    meta = seeds["seed/honest-fix"]
    findings = differential(base_tree, meta["base"], meta["head"])
    assert any(f.symbol.endswith("split_arg_string") for f in findings)


# --------------------------------------------------------------------------------------
# declared_scope
# --------------------------------------------------------------------------------------


def test_declared_scope_extracts_named_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        differential_module,
        "complete",
        lambda prompt, **kw: "split_arg_string\n`measure_table`\n",
    )
    assert declared_scope("Fix split_arg_string", "body") == [
        "split_arg_string",
        "measure_table",
    ]


def test_declared_scope_is_empty_without_a_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(prompt: str, **kwargs: object) -> str:
        raise AssertionError("must not call the model for an empty description")

    monkeypatch.setattr(differential_module, "complete", explode)
    assert declared_scope("", "") == []
