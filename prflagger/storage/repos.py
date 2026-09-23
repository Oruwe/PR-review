"""Typed reads and writes over the tables.

Everything above this module speaks in `core.models` types; everything below is
SQL. Keeping the boundary here is what stops row dicts leaking into the engine
and the API, where a renamed column would then fail silently.
"""

from __future__ import annotations

import json
import time
from typing import Any

from prflagger.core.models import (
    TERMINAL_STATES,
    Adjudication,
    Citation,
    Observation,
    PullRequest,
    Repo,
    Run,
    RunState,
    Suggestion,
    Symbol,
)
from prflagger.storage.db import Database, connect, json_col

__all__ = ["Store"]


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback
    return value if value is not None else fallback


class Store:
    """The data access layer. One instance per process is fine."""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or connect()

    # -- repos ----------------------------------------------------------------

    def put_repo(self, repo: Repo) -> Repo:
        self.db.execute(
            """
            INSERT INTO repos (slug, default_branch, toolchain_id, package_roots,
                               added_at, atlas_sha)
                 VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                default_branch = excluded.default_branch,
                toolchain_id   = excluded.toolchain_id,
                package_roots  = excluded.package_roots
            """,
            (
                repo.slug,
                repo.default_branch,
                repo.toolchain_id,
                json_col(list(repo.package_roots)),
                repo.added_at or time.time(),
                repo.atlas_sha,
            ),
        )
        return self.repo(repo.slug) or repo

    def repo(self, slug: str) -> Repo | None:
        row = self.db.one("SELECT * FROM repos WHERE slug = ?", (slug,))
        return self._repo(row) if row else None

    def repos(self) -> list[Repo]:
        return [self._repo(r) for r in self.db.query("SELECT * FROM repos ORDER BY slug")]

    def delete_repo(self, slug: str) -> None:
        self.db.execute("DELETE FROM repos WHERE slug = ?", (slug,))

    def set_atlas_sha(self, slug: str, sha: str) -> None:
        self.db.execute(
            "UPDATE repos SET atlas_sha = ?, atlas_built_at = ? WHERE slug = ?",
            (sha, time.time(), slug),
        )

    @staticmethod
    def _repo(row: Any) -> Repo:
        return Repo(
            slug=row["slug"],
            default_branch=row["default_branch"],
            toolchain_id=row["toolchain_id"],
            package_roots=tuple(_loads(row["package_roots"], [])),
            added_at=row["added_at"],
            atlas_sha=row["atlas_sha"],
        )

    # -- pulls ----------------------------------------------------------------

    def put_pull(self, pull: PullRequest) -> None:
        self.db.execute(
            """
            INSERT INTO pulls (repo, number, title, body, author, base_sha, head_sha,
                               state, updated_at, additions, deletions, changed_files,
                               draft, seen_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, number) DO UPDATE SET
                title = excluded.title, body = excluded.body, author = excluded.author,
                base_sha = excluded.base_sha, head_sha = excluded.head_sha,
                state = excluded.state, updated_at = excluded.updated_at,
                additions = excluded.additions, deletions = excluded.deletions,
                changed_files = excluded.changed_files, draft = excluded.draft,
                seen_at = excluded.seen_at
            """,
            (
                pull.repo, pull.number, pull.title, pull.body, pull.author,
                pull.base_sha, pull.head_sha, pull.state, pull.updated_at,
                pull.additions, pull.deletions, pull.changed_files,
                int(pull.draft), time.time(),
            ),
        )

    def pull(self, repo: str, number: int) -> PullRequest | None:
        row = self.db.one("SELECT * FROM pulls WHERE repo = ? AND number = ?", (repo, number))
        return self._pull(row) if row else None

    def pulls(self, repo: str, *, state: str | None = "open") -> list[PullRequest]:
        if state:
            rows = self.db.query(
                "SELECT * FROM pulls WHERE repo = ? AND state = ? ORDER BY number DESC",
                (repo, state),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM pulls WHERE repo = ? ORDER BY number DESC", (repo,)
            )
        return [self._pull(r) for r in rows]

    @staticmethod
    def _pull(row: Any) -> PullRequest:
        return PullRequest(
            repo=row["repo"], number=row["number"], title=row["title"], body=row["body"],
            author=row["author"], base_sha=row["base_sha"], head_sha=row["head_sha"],
            state=row["state"], updated_at=row["updated_at"], additions=row["additions"],
            deletions=row["deletions"], changed_files=row["changed_files"],
            draft=bool(row["draft"]),
        )

    # -- runs -----------------------------------------------------------------

    def put_run(self, run: Run) -> None:
        self.db.execute(
            """
            INSERT INTO runs (id, repo, pr_number, base_sha, head_sha, state, trigger,
                              created_at, started_at, finished_at, error, usd_spent)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state, started_at = excluded.started_at,
                finished_at = excluded.finished_at, error = excluded.error,
                usd_spent = excluded.usd_spent
            """,
            (
                run.id, run.repo, run.pr_number, run.base_sha, run.head_sha,
                run.state.value, run.trigger, run.created_at or time.time(),
                run.started_at, run.finished_at, run.error, run.usd_spent,
            ),
        )

    def set_run_state(self, run_id: str, state: RunState, *, error: str = "") -> None:
        now = time.time()
        started = "started_at = COALESCE(started_at, ?)," if state != RunState.QUEUED else ""
        finished = now if state in TERMINAL_STATES else None
        params: list[Any] = []
        if started:
            params.append(now)
        params += [finished, state.value, error, run_id]
        self.db.execute(
            f"UPDATE runs SET {started} finished_at = COALESCE(?, finished_at), "
            "state = ?, error = ? WHERE id = ?",
            params,
        )

    def run(self, run_id: str) -> Run | None:
        row = self.db.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run(row) if row else None

    def runs(
        self, *, repo: str | None = None, pr: int | None = None, limit: int = 50
    ) -> list[Run]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if repo:
            sql += " AND repo = ?"
            params.append(repo)
        if pr is not None:
            sql += " AND pr_number = ?"
            params.append(pr)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._run(r) for r in self.db.query(sql, params)]

    def latest_run(self, repo: str, pr: int) -> Run | None:
        runs = self.runs(repo=repo, pr=pr, limit=1)
        return runs[0] if runs else None

    def unfinished_runs(self) -> list[Run]:
        """Runs left in flight — what `engine.recovery` requeues on boot."""
        terminal = tuple(s.value for s in TERMINAL_STATES)
        placeholders = ",".join("?" * len(terminal))
        return [
            self._run(r)
            for r in self.db.query(
                f"SELECT * FROM runs WHERE state NOT IN ({placeholders}) ORDER BY created_at",
                terminal,
            )
        ]

    def supersede(self, run_id: str, by: str) -> None:
        self.db.execute("UPDATE runs SET superseded_by = ? WHERE id = ?", (by, run_id))

    @staticmethod
    def _run(row: Any) -> Run:
        return Run(
            id=row["id"], repo=row["repo"], pr_number=row["pr_number"],
            base_sha=row["base_sha"], head_sha=row["head_sha"],
            state=RunState(row["state"]), trigger=row["trigger"],
            created_at=row["created_at"], started_at=row["started_at"],
            finished_at=row["finished_at"], error=row["error"],
            usd_spent=row["usd_spent"],
        )

    # -- jobs -----------------------------------------------------------------

    def put_job(self, **fields: Any) -> None:
        columns = (
            "id", "run_id", "idempotency_key", "stage", "image_tag", "argv", "outcome",
            "exit_code", "duration_s", "peak_rss_mb", "memory_mb", "timeout_s",
            "log_path", "started_at", "finished_at",
        )
        values = [fields.get(c) for c in columns]
        assignments = ", ".join(f"{c} = excluded.{c}" for c in columns[2:])
        self.db.execute(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )

    def jobs(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM jobs WHERE run_id = ? ORDER BY started_at, id", (run_id,)
        )]

    # -- observations and what annotates them ---------------------------------

    def put_observations(self, observations: list[Observation]) -> None:
        self.db.executemany(
            """
            INSERT INTO observations (id, run_id, kind, symbol, what_changed, how_we_know,
                                      evidence_ref, severity, confidence, rank_score)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET rank_score = excluded.rank_score
            """,
            [
                (o.id, o.run_id, o.kind, o.symbol, o.what_changed, o.how_we_know,
                 o.evidence_ref, o.severity, o.confidence, o.rank_score)
                for o in observations
            ],
        )

    def observations(self, run_id: str) -> list[Observation]:
        rows = self.db.query(
            "SELECT * FROM observations WHERE run_id = ? ORDER BY rank_score DESC, id",
            (run_id,),
        )
        return [
            Observation(
                id=r["id"], run_id=r["run_id"], kind=r["kind"], symbol=r["symbol"],
                what_changed=r["what_changed"], how_we_know=r["how_we_know"],
                evidence_ref=r["evidence_ref"], severity=r["severity"],
                confidence=r["confidence"], rank_score=r["rank_score"],
            )
            for r in rows
        ]

    def put_adjudication(self, adj: Adjudication) -> None:
        self.db.execute(
            """
            INSERT INTO adjudications (observation_id, assessment, reasoning, citations,
                                       model, usd)
                 VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(observation_id) DO UPDATE SET
                assessment = excluded.assessment, reasoning = excluded.reasoning,
                citations = excluded.citations, model = excluded.model, usd = excluded.usd
            """,
            (
                adj.observation_id, adj.assessment, adj.reasoning,
                json_col([c.__dict__ for c in adj.citations]), adj.model, adj.usd,
            ),
        )

    def adjudications(self, run_id: str) -> dict[str, Adjudication]:
        rows = self.db.query(
            """
            SELECT a.* FROM adjudications a
              JOIN observations o ON o.id = a.observation_id
             WHERE o.run_id = ?
            """,
            (run_id,),
        )
        out: dict[str, Adjudication] = {}
        for r in rows:
            citations = tuple(
                Citation(type=c["type"], ref=c["ref"], quote=c.get("quote", ""))
                for c in _loads(r["citations"], [])
            )
            if not citations:
                continue  # an uncited row cannot be reconstructed; it is not shown
            out[r["observation_id"]] = Adjudication(
                observation_id=r["observation_id"], assessment=r["assessment"],
                reasoning=r["reasoning"], citations=citations, model=r["model"],
                usd=r["usd"],
            )
        return out

    def put_suggestion(self, suggestion: Suggestion) -> None:
        self.db.execute(
            """
            INSERT INTO suggestions (observation_id, summary, rationale, patch_sketch,
                                     confidence, citations)
                 VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(observation_id) DO UPDATE SET
                summary = excluded.summary, rationale = excluded.rationale,
                patch_sketch = excluded.patch_sketch, confidence = excluded.confidence,
                citations = excluded.citations
            """,
            (
                suggestion.observation_id, suggestion.summary, suggestion.rationale,
                suggestion.patch_sketch, suggestion.confidence,
                json_col([c.__dict__ for c in suggestion.citations]),
            ),
        )

    def suggestions(self, run_id: str) -> dict[str, Suggestion]:
        rows = self.db.query(
            """
            SELECT s.* FROM suggestions s
              JOIN observations o ON o.id = s.observation_id
             WHERE o.run_id = ?
            """,
            (run_id,),
        )
        out: dict[str, Suggestion] = {}
        for r in rows:
            citations = tuple(
                Citation(type=c["type"], ref=c["ref"], quote=c.get("quote", ""))
                for c in _loads(r["citations"], [])
            )
            if not citations:
                continue
            out[r["observation_id"]] = Suggestion(
                observation_id=r["observation_id"], summary=r["summary"],
                rationale=r["rationale"], patch_sketch=r["patch_sketch"],
                confidence=r["confidence"], citations=citations,
            )
        return out

    # -- symbols and call graph -----------------------------------------------

    def put_symbols(self, repo: str, symbols: list[Symbol]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO symbols (repo, fqn, kind, file, line_start, line_end, lang)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(repo, s.fqn, s.kind, s.file, s.line_start, s.line_end, s.lang) for s in symbols],
        )

    def put_call_edges(self, repo: str, edges: dict[str, set[str]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO call_edges (repo, caller, callee) VALUES (?, ?, ?)",
            [(repo, caller, callee) for caller, callees in edges.items() for callee in callees],
        )

    def caller_counts(self, repo: str) -> dict[str, int]:
        """How many distinct callers each symbol has. Feeds ranking's centrality."""
        return {
            r["callee"]: int(r["n"])
            for r in self.db.query(
                "SELECT callee, COUNT(DISTINCT caller) AS n FROM call_edges "
                "WHERE repo = ? GROUP BY callee",
                (repo,),
            )
        }

    # -- atlas ----------------------------------------------------------------

    def put_atlas(self, repo: str, sha: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO atlases (repo, sha, built_at, payload) VALUES (?, ?, ?, ?)",
            (repo, sha, time.time(), json_col(payload)),
        )
        self.set_atlas_sha(repo, sha)

    def atlas(self, repo: str, sha: str | None = None) -> dict[str, Any] | None:
        if sha:
            row = self.db.one("SELECT * FROM atlases WHERE repo = ? AND sha = ?", (repo, sha))
        else:
            row = self.db.one(
                "SELECT * FROM atlases WHERE repo = ? ORDER BY built_at DESC LIMIT 1", (repo,)
            )
        if row is None:
            return None
        payload = _loads(row["payload"], None)
        return payload if isinstance(payload, dict) else None
