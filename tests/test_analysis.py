"""C2 acceptance.

SPEC.md § C2: `tests/fixtures/toypkg/` is a synthetic package with a known call structure
(`a()` calls `b()` calls `c()`, plus an unrelated `d()`). Asserts: `build_call_graph`
returns exactly the expected edge set; `blast_radius` on a commit touching `c()` returns
`{c, b, a}` and excludes `d`. Then a smoke assert on the real target repo: `blast_radius`
on any real commit returns a non-empty list in under 30 seconds.

The toy repo is a real git repository built from the fixture — git is never mocked.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from prflagger.analysis.blast import blast_radius, changed_symbols, package_root_for
from prflagger.analysis.callgraph import build_call_graph, index_symbols
from prflagger.analysis.diff import changed_ranges
from prflagger.analysis.symbols import module_fqn_for, symbols_in_file
from tests.conftest import git_in

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# a() calls b() calls c(). d() calls nothing and nothing calls d().
EXPECTED_EDGES = {
    ("toypkg.top.a", "toypkg.middle.b"),
    ("toypkg.middle.b", "toypkg.leaf.c"),
}


# --------------------------------------------------------------------------------------
# build_call_graph returns exactly the expected edge set
# --------------------------------------------------------------------------------------


def test_call_graph_is_exactly_the_expected_edge_set() -> None:
    graph = build_call_graph(FIXTURES / "toypkg")
    edges = {(caller, callee) for caller, callees in graph.items() for callee in callees}
    assert edges == EXPECTED_EDGES


def test_call_graph_resolves_across_modules() -> None:
    graph = build_call_graph(FIXTURES / "toypkg")
    # The edge exists because `from toypkg.leaf import c` was resolved, not guessed.
    assert graph["toypkg.middle.b"] == {"toypkg.leaf.c"}
    assert "toypkg.unrelated.d" not in graph


# --------------------------------------------------------------------------------------
# blast_radius on a commit touching c() is {c, b, a} and excludes d
# --------------------------------------------------------------------------------------


def test_blast_radius_reaches_transitive_callers_and_excludes_the_unrelated(
    toy_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = toy_repo
    fqns = {symbol.fqn for symbol in blast_radius(repo, base, head)}

    assert fqns == {"toypkg.leaf.c", "toypkg.middle.b", "toypkg.top.a"}
    assert "toypkg.unrelated.d" not in fqns


def test_one_hop_stops_before_the_second_caller(toy_repo: tuple[Path, str, str]) -> None:
    repo, base, head = toy_repo
    fqns = {symbol.fqn for symbol in blast_radius(repo, base, head, hops=1)}
    # c is touched, b calls c. a is two hops away and must not be reached.
    assert fqns == {"toypkg.leaf.c", "toypkg.middle.b"}


def test_blast_radius_symbols_carry_repo_relative_paths(
    toy_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = toy_repo
    for symbol in blast_radius(repo, base, head):
        assert not symbol.file.startswith("/"), symbol.file
        assert (repo / symbol.file).is_file()


def test_changed_symbols_is_only_what_the_diff_touched(
    toy_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = toy_repo
    assert {s.fqn for s in changed_symbols(repo, base, head)} == {"toypkg.leaf.c"}


# --------------------------------------------------------------------------------------
# changed_ranges
# --------------------------------------------------------------------------------------


def test_changed_ranges_reports_head_side_lines(toy_repo: tuple[Path, str, str]) -> None:
    repo, base, head = toy_repo
    ranges = changed_ranges(repo, base, head)

    assert set(ranges) == {"toypkg/leaf.py"}
    (start, end) = ranges["toypkg/leaf.py"][0]
    line = (repo / "toypkg/leaf.py").read_text(encoding="utf-8").splitlines()[start - 1]
    assert "value * 3" in line
    assert start <= end


def test_a_deleted_file_contributes_no_head_ranges(toy_repo: tuple[Path, str, str]) -> None:
    repo, base, _ = toy_repo
    (repo / "toypkg" / "unrelated.py").unlink()
    git_in(repo, "commit", "--quiet", "-am", "drop unrelated")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert "toypkg/unrelated.py" not in changed_ranges(repo, base=f"{head}~1", head=head)


# --------------------------------------------------------------------------------------
# symbols_in_file
# --------------------------------------------------------------------------------------


def test_symbols_cover_nesting_methods_and_async() -> None:
    symbols = {s.fqn: s for s in symbols_in_file(FIXTURES / "shapes.py", "shapes")}

    assert symbols["shapes.plain"].kind == "function"
    assert symbols["shapes.coroutine"].kind == "function"
    assert symbols["shapes.Holder"].kind == "class"
    assert symbols["shapes.Holder.method"].kind == "method"
    assert symbols["shapes.Holder.Inner"].kind == "class"
    assert symbols["shapes.Holder.Inner.deep"].kind == "method"
    # Nested functions get dotted fqns.
    assert "shapes.Holder.method.nested" in symbols


def test_a_decorator_does_not_create_a_symbol_or_widen_one() -> None:
    source = (FIXTURES / "shapes.py").read_text(encoding="utf-8").splitlines()
    symbols = {s.fqn: s for s in symbols_in_file(FIXTURES / "shapes.py", "shapes")}

    assert "shapes.functools.cache" not in symbols
    assert "shapes.cache" not in symbols
    # The range starts at `def`, not at the decorator above it.
    assert source[symbols["shapes.decorated"].line_start - 1].startswith("def decorated")
    assert source[symbols["shapes.Holder.prop"].line_start - 1].strip().startswith("def prop")


def test_module_fqn_follows_the_package_layout() -> None:
    root = FIXTURES / "toypkg"
    assert module_fqn_for(root / "leaf.py", root) == "toypkg.leaf"
    assert module_fqn_for(root / "__init__.py", root) == "toypkg"


# --------------------------------------------------------------------------------------
# Smoke assert on the real target repo
# --------------------------------------------------------------------------------------


def _recent_commit_touching_package(repo: Path, package_root: Path, limit: int = 40) -> str:
    """A commit that actually changed the package.

    A commit touching only docs or tests has an empty radius, which is correct behaviour
    rather than a failure — so the smoke assert must not depend on what HEAD happens to be.
    """
    relative = package_root.resolve().relative_to(repo.resolve()).as_posix()
    revisions = subprocess.run(
        ["git", "-C", str(repo), "log", "-n", str(limit), "--format=%H", "--", relative],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for revision in revisions:
        if changed_symbols(repo, f"{revision}~1", revision):
            return revision
    raise AssertionError(f"no commit in the last {limit} touched {relative}")


def test_blast_radius_on_the_real_target_repo_is_fast_and_non_empty(
    head_worktree: Path,
) -> None:
    revision = _recent_commit_touching_package(
        head_worktree, package_root_for(head_worktree)
    )

    started = time.monotonic()
    radius = blast_radius(head_worktree, f"{revision}~1", revision)
    elapsed = time.monotonic() - started

    assert len(radius) > 0
    assert elapsed < 30, f"took {elapsed:.1f}s"
    # The radius is a working set, not the whole package: if it were, it would be useless.
    assert len(radius) < len(index_symbols(package_root_for(head_worktree)))


def test_real_target_package_root_and_index(head_worktree: Path) -> None:
    root = package_root_for(head_worktree)
    assert root.name == "click"
    index = index_symbols(root)
    # A real package has real symbols; this is the map blast radius walks.
    assert len(index) > 100
    assert any(fqn.startswith("click.") for fqn in index)
