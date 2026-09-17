"""C8 acceptance.

SPEC.md § C8: write then read back symbols, edges and norms across a process restart.
`callers_of` returns the same set as `blast_radius`'s graph traversal for the toy
fixture. `match_norm("needs a test for this")` ranks a test-related norm first.

The third criterion needs the local embedding model, which this environment cannot
fetch (huggingface.co is refused by egress policy). It is marked with that reason
rather than quietly relaxed — see `test_match_norm_ranks_a_test_norm_first`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from prflagger.analysis.blast import blast_radius
from prflagger.analysis.callgraph import build_call_graph, index_symbols
from prflagger.brain.store import KnowledgeStore, SqliteGraphStore
from prflagger.llm import embeddings_available
from prflagger.models import Norm, Symbol

REPO = "pallets/click"

NORMS = [
    Norm(
        id="test-for-new-public-fn",
        statement="New public functions require a test in the same pull request.",
        scope="repo",
        support=23,
        distinct_reviewers=4,
        confidence=1.0,
        evidence_prs=(412, 457, 490),
    ),
    Norm(
        id="no-bare-except",
        statement="Never catch a bare except; name the exception type.",
        scope="repo",
        support=7,
        distinct_reviewers=3,
        confidence=0.52,
        evidence_prs=(311, 402),
    ),
    Norm(
        id="error-messages-name-the-value",
        statement="Error messages must name the offending value.",
        scope="project",
        support=5,
        distinct_reviewers=2,
        confidence=0.25,
        evidence_prs=(200,),
    ),
]

def _symbol(fqn: str, module: str) -> Symbol:
    return Symbol(
        fqn=fqn, kind="function", file=f"toypkg/{module}.py", line_start=4, line_end=6
    )


SYMBOLS = [
    _symbol("toypkg.leaf.c", "leaf"),
    _symbol("toypkg.middle.b", "middle"),
    _symbol("toypkg.top.a", "top"),
]


def test_the_protocol_is_satisfied(tmp_path: Path) -> None:
    store: KnowledgeStore = SqliteGraphStore(tmp_path / "brain.sqlite")
    assert isinstance(store, SqliteGraphStore)


# --------------------------------------------------------------------------------------
# Round trip across a process restart
# --------------------------------------------------------------------------------------

_WRITER = """
import sys
from pathlib import Path
from prflagger.brain.store import SqliteGraphStore
from prflagger.models import Norm, Symbol

store = SqliteGraphStore(Path(sys.argv[1]))
def s(fqn, module):
    return Symbol(fqn=fqn, kind="function", file="toypkg/" + module + ".py",
                  line_start=4, line_end=6)

store.put_symbols({repo!r}, [
    s("toypkg.leaf.c", "leaf"),
    s("toypkg.middle.b", "middle"),
    s("toypkg.top.a", "top"),
])
store.put_call_edges({repo!r}, {{
    "toypkg.top.a": {{"toypkg.middle.b"}},
    "toypkg.middle.b": {{"toypkg.leaf.c"}},
}})
store.put_norms({repo!r}, [
    Norm(id="test-for-new-public-fn",
         statement="New public functions require a test in the same pull request.",
         scope="repo", support=23, distinct_reviewers=4, confidence=1.0,
         evidence_prs=(412, 457, 490)),
])
store.close()
"""


def test_everything_round_trips_across_a_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "brain.sqlite"
    written = subprocess.run(
        [sys.executable, "-c", _WRITER.format(repo=REPO), str(database)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert written.returncode == 0, written.stderr

    # A different process entirely: nothing survives in memory.
    store = SqliteGraphStore(database)

    norms = store.norms_for(REPO)
    assert [norm.id for norm in norms] == ["test-for-new-public-fn"]
    assert norms[0].evidence_prs == (412, 457, 490)
    assert norms[0].scope == "repo"  # persisted even though only "repo" is used today
    assert norms[0].support == 23
    assert norms[0].confidence == pytest.approx(1.0)

    callers = store.callers_of(REPO, "toypkg.leaf.c", hops=2)
    assert {symbol.fqn for symbol in callers} == {"toypkg.middle.b", "toypkg.top.a"}
    # Symbols came back whole, not just their names.
    assert callers[0].file.endswith(".py")
    assert callers[0].line_start > 0


def test_writes_are_idempotent(tmp_path: Path) -> None:
    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    for _ in range(2):
        store.put_symbols(REPO, SYMBOLS)
        store.put_call_edges(REPO, {"toypkg.top.a": {"toypkg.middle.b"}})
        store.put_norms(REPO, NORMS)
    assert len(store.norms_for(REPO)) == len(NORMS)
    assert len(store.callers_of(REPO, "toypkg.middle.b", hops=1)) == 1


def test_repos_are_isolated(tmp_path: Path) -> None:
    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    store.put_norms(REPO, NORMS)
    assert store.norms_for("someone/else") == []


# --------------------------------------------------------------------------------------
# callers_of agrees with blast_radius's traversal
# --------------------------------------------------------------------------------------


def test_callers_of_matches_blast_radius_on_the_toy_fixture(
    toy_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, base, head = toy_repo
    package_root = repo / "toypkg"

    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    store.put_symbols(REPO, list(index_symbols(package_root).values()))
    store.put_call_edges(REPO, build_call_graph(package_root))

    from_graph = {symbol.fqn for symbol in store.callers_of(REPO, "toypkg.leaf.c", hops=2)}
    from_blast = {symbol.fqn for symbol in blast_radius(repo, base, head, hops=2)}

    # blast_radius includes the changed symbol itself; callers_of returns only callers.
    assert from_graph | {"toypkg.leaf.c"} == from_blast
    assert "toypkg.unrelated.d" not in from_graph


def test_callers_of_respects_the_hop_limit(
    toy_repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repo, _, _ = toy_repo
    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    store.put_symbols(REPO, list(index_symbols(repo / "toypkg").values()))
    store.put_call_edges(REPO, build_call_graph(repo / "toypkg"))

    one_hop = {s.fqn for s in store.callers_of(REPO, "toypkg.leaf.c", hops=1)}
    assert one_hop == {"toypkg.middle.b"}
    assert store.callers_of(REPO, "toypkg.unrelated.d", hops=2) == []
    assert store.callers_of(REPO, "nope.not.here", hops=2) == []


# --------------------------------------------------------------------------------------
# match_norm
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not embeddings_available(),
    reason=(
        "all-MiniLM-L6-v2 cannot be fetched here: huggingface.co is refused by this "
        "environment's egress policy. This acceptance criterion is UNMET, not relaxed."
    ),
)
def test_match_norm_ranks_a_test_norm_first(tmp_path: Path) -> None:
    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    store.put_norms(REPO, NORMS)

    ranked = store.match_norm(REPO, "needs a test for this", k=3)

    assert ranked, "a populated store must rank something"
    assert ranked[0][0].id == "test-for-new-public-fn"
    assert ranked[0][1] > 0.0
    assert ranked == sorted(ranked, key=lambda pair: -pair[1])


def test_match_norm_returns_nothing_rather_than_ranking_by_another_measure(
    tmp_path: Path,
) -> None:
    """Explicit degradation: without embeddings the store declines to rank.

    Substituting a different vectoriser would produce a citation this system cannot
    stand behind, which is worse than producing none.
    """
    store = SqliteGraphStore(tmp_path / "brain.sqlite")
    store.put_norms(REPO, NORMS)

    ranked = store.match_norm(REPO, "needs a test for this")
    if embeddings_available():
        assert ranked
    else:
        assert ranked == []
        # The norms themselves are still stored and readable.
        assert len(store.norms_for(REPO)) == len(NORMS)
