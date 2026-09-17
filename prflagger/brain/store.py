"""Knowledge storage behind a six-method protocol.

NetworkX in memory, SQLite for persistence, a numpy array for embeddings. The protocol
is what makes "we would swap in Neo4j" a true statement rather than a hope: the
production target is a different implementation of these six methods, not a migration.

`Norm.scope` is persisted even though only "repo" is used today. Keeping it is what
makes org-level inheritance a query change rather than a schema change.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import networkx as nx
import numpy as np
import structlog

from prflagger.llm import EmbeddingUnavailable, embed
from prflagger.models import Norm, Symbol

__all__ = ["KnowledgeStore", "SqliteGraphStore"]

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    repo TEXT NOT NULL,
    fqn TEXT NOT NULL,
    kind TEXT NOT NULL,
    file TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    PRIMARY KEY (repo, fqn)
);
CREATE TABLE IF NOT EXISTS call_edges (
    repo TEXT NOT NULL,
    caller TEXT NOT NULL,
    callee TEXT NOT NULL,
    PRIMARY KEY (repo, caller, callee)
);
CREATE TABLE IF NOT EXISTS norms (
    repo TEXT NOT NULL,
    id TEXT NOT NULL,
    statement TEXT NOT NULL,
    scope TEXT NOT NULL,
    support INTEGER NOT NULL,
    distinct_reviewers INTEGER NOT NULL,
    confidence REAL NOT NULL,
    evidence_prs TEXT NOT NULL,
    embedding BLOB,
    PRIMARY KEY (repo, id)
);
CREATE INDEX IF NOT EXISTS call_edges_callee ON call_edges (repo, callee);
"""


class KnowledgeStore(Protocol):
    def put_symbols(self, repo: str, symbols: list[Symbol]) -> None: ...
    def put_call_edges(self, repo: str, edges: dict[str, set[str]]) -> None: ...
    def put_norms(self, repo: str, norms: list[Norm]) -> None: ...
    def callers_of(self, repo: str, fqn: str, hops: int) -> list[Symbol]: ...
    def norms_for(self, repo: str) -> list[Norm]: ...
    def match_norm(self, repo: str, text: str, k: int = 3) -> list[tuple[Norm, float]]: ...


class SqliteGraphStore:
    """NetworkX in memory, SQLite for persistence, numpy array for embeddings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        self._graphs: dict[str, nx.DiGraph[str]] = {}

    # -- writes ---------------------------------------------------------------------

    def put_symbols(self, repo: str, symbols: list[Symbol]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO symbols VALUES (?, ?, ?, ?, ?, ?)",
            [
                (repo, s.fqn, s.kind, s.file, s.line_start, s.line_end)
                for s in symbols
            ],
        )
        self._connection.commit()

    def put_call_edges(self, repo: str, edges: dict[str, set[str]]) -> None:
        rows = [
            (repo, caller, callee)
            for caller, callees in edges.items()
            for callee in callees
        ]
        self._connection.executemany(
            "INSERT OR REPLACE INTO call_edges VALUES (?, ?, ?)", rows
        )
        self._connection.commit()
        self._graphs.pop(repo, None)

    def put_norms(self, repo: str, norms: list[Norm]) -> None:
        """Norms are stored whether or not they can be embedded.

        Without embeddings `match_norm` cannot rank them, and says so by returning
        nothing rather than ranking by some other measure.
        """
        if not norms:
            return
        try:
            vectors: list[list[float]] = embed([norm.statement for norm in norms])
        except EmbeddingUnavailable:
            log.warning("norms.stored_without_embeddings", repo=repo, count=len(norms))
            vectors = [[] for _ in norms]
        self._connection.executemany(
            "INSERT OR REPLACE INTO norms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    repo,
                    norm.id,
                    norm.statement,
                    norm.scope,
                    norm.support,
                    norm.distinct_reviewers,
                    norm.confidence,
                    json.dumps(list(norm.evidence_prs)),
                    np.asarray(vector, dtype=np.float32).tobytes(),
                )
                for norm, vector in zip(norms, vectors, strict=True)
            ],
        )
        self._connection.commit()

    # -- reads ----------------------------------------------------------------------

    def callers_of(self, repo: str, fqn: str, hops: int) -> list[Symbol]:
        graph = self._graph(repo)
        if fqn not in graph:
            return []
        symbols = self._symbols(repo)
        reached: set[str] = set()
        frontier = deque([(fqn, 0)])
        while frontier:
            current, depth = frontier.popleft()
            if depth >= hops:
                continue
            for caller in graph.predecessors(current):
                if caller not in reached and caller != fqn:
                    reached.add(caller)
                    frontier.append((caller, depth + 1))
        return [symbols[name] for name in sorted(reached) if name in symbols]

    def norms_for(self, repo: str) -> list[Norm]:
        rows = self._connection.execute(
            "SELECT id, statement, scope, support, distinct_reviewers, confidence,"
            " evidence_prs FROM norms WHERE repo = ? ORDER BY id",
            (repo,),
        ).fetchall()
        return [_norm(row) for row in rows]

    def match_norm(self, repo: str, text: str, k: int = 3) -> list[tuple[Norm, float]]:
        """Cosine similarity against the repo's norm embeddings, best first."""
        rows = self._connection.execute(
            "SELECT id, statement, scope, support, distinct_reviewers, confidence,"
            " evidence_prs, embedding FROM norms WHERE repo = ? ORDER BY id",
            (repo,),
        ).fetchall()
        rows = [row for row in rows if row[7]]
        if not rows:
            return []

        matrix = np.vstack([np.frombuffer(row[7], dtype=np.float32) for row in rows])
        try:
            query = np.asarray(embed([text])[0], dtype=np.float32)
        except EmbeddingUnavailable:
            log.warning("match_norm.unavailable", repo=repo)
            return []
        scores = matrix @ query / (
            np.linalg.norm(matrix, axis=1) * np.linalg.norm(query) + 1e-12
        )
        order = np.argsort(-scores)[:k]
        return [(_norm(rows[index]), float(scores[index])) for index in order]

    # -- internals ------------------------------------------------------------------

    def _symbols(self, repo: str) -> dict[str, Symbol]:
        rows = self._connection.execute(
            "SELECT fqn, kind, file, line_start, line_end FROM symbols WHERE repo = ?",
            (repo,),
        ).fetchall()
        return {
            row[0]: Symbol(
                fqn=row[0], kind=row[1], file=row[2], line_start=row[3], line_end=row[4]
            )
            for row in rows
        }

    def _graph(self, repo: str) -> nx.DiGraph[str]:
        cached = self._graphs.get(repo)
        if cached is not None:
            return cached
        graph: nx.DiGraph[str] = nx.DiGraph()
        for caller, callee in self._connection.execute(
            "SELECT caller, callee FROM call_edges WHERE repo = ?", (repo,)
        ):
            graph.add_edge(caller, callee)
        self._graphs[repo] = graph
        return graph

    def close(self) -> None:
        self._connection.close()


def _norm(row: Sequence[Any]) -> Norm:
    return Norm(
        id=str(row[0]),
        statement=str(row[1]),
        scope=str(row[2]),
        support=int(row[3]),
        distinct_reviewers=int(row[4]),
        confidence=float(row[5]),
        evidence_prs=tuple(int(number) for number in json.loads(str(row[6]))),
    )
