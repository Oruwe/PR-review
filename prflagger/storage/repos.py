"""Typed reads and writes over the tables.

Everything above this module speaks in `core.models` types; everything below is
SQL. Keeping the boundary here is what stops row dicts leaking into the engine
and the API, where a renamed column would then fail silently.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

from prflagger.core.models import (
    DRIFT_LEVELS,
    TERMINAL_STATES,
    Adjudication,
    Charter,
    CharterDrift,
    Citation,
    Claim,
    Norm,
    Notification,
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

    def set_branch_head(self, slug: str, sha: str) -> None:
        self.db.execute("UPDATE repos SET branch_head = ? WHERE slug = ?", (sha, slug))

    @staticmethod
    def _repo(row: Any) -> Repo:
        keys = row.keys()
        return Repo(
            slug=row["slug"],
            default_branch=row["default_branch"],
            toolchain_id=row["toolchain_id"],
            package_roots=tuple(_loads(row["package_roots"], [])),
            added_at=row["added_at"],
            atlas_sha=row["atlas_sha"],
            charter_sha=row["charter_sha"] if "charter_sha" in keys else "",
            branch_head=row["branch_head"] if "branch_head" in keys else "",
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

    def retarget(self, run_id: str, base_sha: str, head_sha: str) -> None:
        """Point a not-yet-started run at different commits.

        Used when a PR is force-pushed while its run is still queued: the run
        must verify the newest commit, not the one that happened to be current
        when it was created.
        """
        self.db.execute(
            "UPDATE runs SET base_sha = ?, head_sha = ? WHERE id = ? AND state = 'queued'",
            (base_sha, head_sha, run_id),
        )

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
        # Columns declared NOT NULL DEFAULT '' still reject an explicit NULL, so a
        # field the caller did not supply has to become the default here rather
        # than being bound as None.
        text_columns = ("idempotency_key", "image_tag", "log_path")
        payload = dict(fields)
        payload["argv"] = json_col(list(payload.get("argv") or []))
        for column in text_columns:
            payload[column] = payload.get(column) or ""
        payload["stage"] = payload.get("stage") or "unknown"
        values = [payload.get(c) for c in columns]
        assignments = ", ".join(f"{c} = excluded.{c}" for c in columns[2:])
        self.db.execute(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )

    def jobs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM jobs WHERE run_id = ? ORDER BY started_at, id", (run_id,)
        )
        out = []
        for row in rows:
            entry = dict(row)
            entry["argv"] = _loads(entry.get("argv"), [])
            out.append(entry)
        return out

    # -- observations and what annotates them ---------------------------------

    def put_observations(self, observations: list[Observation]) -> None:
        self.db.executemany(
            """
            INSERT INTO observations (id, run_id, kind, symbol, what_changed, how_we_know,
                                      evidence_ref, severity, confidence, rank_score,
                                      relevance, relevance_note, norm_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                rank_score = excluded.rank_score,
                confidence = excluded.confidence,
                relevance = excluded.relevance,
                relevance_note = excluded.relevance_note,
                norm_id = excluded.norm_id
            """,
            [
                (o.id, o.run_id, o.kind, o.symbol, o.what_changed, o.how_we_know,
                 o.evidence_ref, o.severity, o.confidence, o.rank_score,
                 o.relevance, o.relevance_note, o.norm_id or None)
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
                relevance=r["relevance"], relevance_note=r["relevance_note"],
                norm_id=r["norm_id"] or "",
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

    # -- the repository's memory of itself ------------------------------------

    def put_charter(self, charter: Charter) -> Charter:
        """Store a charter, numbering it per repository. Idempotent per commit."""
        existing = self.charter(charter.repo, charter.sha)
        if existing is not None:
            return existing
        number = int(
            self.db.scalar(
                "SELECT MAX(number) FROM charters WHERE repo = ?", (charter.repo,), default=0
            ) or 0
        ) + 1
        stored = replace(charter, number=number, built_at=charter.built_at or time.time())
        self.db.execute(
            "INSERT INTO charters (repo, sha, number, built_at, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (stored.repo, stored.sha, number, stored.built_at, json_col(_charter_dict(stored))),
        )
        self.db.execute(
            "UPDATE repos SET charter_sha = ? WHERE slug = ?", (stored.sha, stored.repo)
        )
        return stored

    def charter(self, repo: str, sha: str | None = None) -> Charter | None:
        """This repository's charter — at `sha`, or its latest. Never another's."""
        if sha:
            row = self.db.one(
                "SELECT * FROM charters WHERE repo = ? AND sha = ?", (repo, sha)
            )
        else:
            row = self.db.one(
                "SELECT * FROM charters WHERE repo = ? ORDER BY number DESC LIMIT 1", (repo,)
            )
        if row is None:
            return None
        charter = _charter_from(_loads(row["payload"], {}), row)
        if charter.repo != repo:  # pragma: no cover - the WHERE clause makes this impossible
            raise RuntimeError(f"charter for {charter.repo!r} returned for {repo!r}")
        return charter

    def charter_history(self, repo: str, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {"number": r["number"], "sha": r["sha"], "built_at": r["built_at"]}
            for r in self.db.query(
                "SELECT number, sha, built_at FROM charters WHERE repo = ? "
                "ORDER BY number DESC LIMIT ?",
                (repo, limit),
            )
        ]

    def put_charter_change(self, drift: CharterDrift) -> int:
        cursor = self.db.execute(
            "INSERT INTO charter_changes (repo, from_sha, to_sha, level, signals, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                drift.repo, drift.from_sha, drift.to_sha, drift.level,
                json_col([s.__dict__ for s in drift.signals]), time.time(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def charter_changes(self, repo: str, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "id": r["id"], "from_sha": r["from_sha"], "to_sha": r["to_sha"],
                "level": r["level"], "signals": _loads(r["signals"], []),
                "created_at": r["created_at"],
            }
            for r in self.db.query(
                "SELECT * FROM charter_changes WHERE repo = ? ORDER BY id DESC LIMIT ?",
                (repo, limit),
            )
        ]

    def set_run_charter_impact(self, run_id: str, level: str) -> None:
        self.db.execute("UPDATE runs SET charter_impact = ? WHERE id = ?", (level, run_id))

    def run_charter_impact(self, run_id: str) -> str:
        return str(
            self.db.scalar(
                "SELECT charter_impact FROM runs WHERE id = ?", (run_id,), default=""
            )
        )

    # -- notifications --------------------------------------------------------

    def put_notification(self, notification: Notification) -> bool:
        """Record a notification. False if this exact one already exists.

        Ids are derived from (repo, kind, sha), so the same update seen twice —
        a restart, a second poll — never notifies twice.
        """
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO notifications
                (id, repo, kind, level, title, body, sha, evidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                notification.id, notification.repo, notification.kind, notification.level,
                notification.title, notification.body, notification.sha,
                json_col(list(notification.evidence)),
                notification.created_at or time.time(),
            ),
        )
        return bool(cursor.rowcount)

    def notifications(
        self,
        *,
        repo: str | None = None,
        open_only: bool = False,
        min_level: str = "none",
        limit: int = 100,
    ) -> list[Notification]:
        floor = DRIFT_LEVELS.index(min_level)
        wanted = [level for level in DRIFT_LEVELS if DRIFT_LEVELS.index(level) >= floor]
        sql = f"SELECT * FROM notifications WHERE level IN ({','.join('?' * len(wanted))})"
        params: list[Any] = list(wanted)
        if repo:
            sql += " AND repo = ?"
            params.append(repo)
        if open_only:
            sql += " AND acknowledged_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [
            Notification(
                id=r["id"], repo=r["repo"], kind=r["kind"], level=r["level"],
                title=r["title"], body=r["body"], sha=r["sha"],
                created_at=r["created_at"], acknowledged_at=r["acknowledged_at"],
                evidence=tuple(_loads(r["evidence"], [])),
            )
            for r in self.db.query(sql, params)
        ]

    def acknowledge(self, notification_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE notifications SET acknowledged_at = ? "
            "WHERE id = ? AND acknowledged_at IS NULL",
            (time.time(), notification_id),
        )
        return bool(cursor.rowcount)

    def record_delivery(self, notification_id: str, delivery: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE notifications SET delivery = ? WHERE id = ?",
            (json_col(delivery), notification_id),
        )


    # -- review history and norms ----------------------------------------------

    def put_review_comments(self, repo: str, judged: list[dict[str, Any]]) -> int:
        """Record judged review comments; returns how many enforced ones were new."""
        before = self.review_counts(repo)[1]
        self.db.executemany(
            """
            INSERT INTO review_comments (repo, comment_key, pr_number, reviewer, body, path,
                                         html_url, created_at, enforced)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, comment_key) DO UPDATE SET
                body = excluded.body, enforced = excluded.enforced
            """,
            [
                (
                    repo, _comment_key(c), int(c["pr_number"]), c["reviewer_login"],
                    c["body"], c.get("path", ""), c.get("html_url", ""), c["created_at"],
                    1 if c["enforced"] else 0,
                )
                for c in judged
            ],
        )
        return self.review_counts(repo)[1] - before

    def review_comments(
        self, repo: str, *, enforced_only: bool = True, limit: int = 2000
    ) -> list[dict[str, Any]]:
        """This repository's review comments, newest first."""
        sql = "SELECT * FROM review_comments WHERE repo = ?"
        if enforced_only:
            sql += " AND enforced = 1"
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [
            {
                "comment_key": r["comment_key"], "pr_number": r["pr_number"],
                "reviewer_login": r["reviewer"], "body": r["body"], "path": r["path"],
                "html_url": r["html_url"], "created_at": r["created_at"],
                "enforced": bool(r["enforced"]),
            }
            for r in self.db.query(sql, (repo, limit))
        ]

    def review_counts(self, repo: str) -> tuple[int, int]:
        """(comments seen, comments enforced) for this repository."""
        row = self.db.one(
            "SELECT COUNT(*) AS seen, COALESCE(SUM(enforced), 0) AS kept"
            " FROM review_comments WHERE repo = ?",
            (repo,),
        )
        return (int(row["seen"]), int(row["kept"])) if row else (0, 0)

    def brain_state(self, repo: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM brain_state WHERE repo = ?", (repo,))
        return dict(row) if row else None

    def put_brain_state(self, repo: str, **fields: Any) -> None:
        allowed = {"harvested_through", "prs_seen", "built_at", "clustered_by",
                   "named_by", "note"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown brain_state fields: {sorted(unknown)}")
        self.db.execute("INSERT OR IGNORE INTO brain_state (repo) VALUES (?)", (repo,))
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            self.db.execute(
                f"UPDATE brain_state SET {assignments} WHERE repo = ?",  # noqa: S608 - names checked above
                (*fields.values(), repo),
            )

    def replace_norms(self, repo: str, source: str, norms: list[Norm]) -> None:
        """Swap this repository's norms from one source for a new set, atomically.

        Declared and mined norms are replaced independently: re-reading the
        configuration must not throw away what was learned from reviews.
        """
        if any(norm.source != source for norm in norms):
            raise ValueError(f"every norm passed must have source={source!r}")
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM norms WHERE repo = ? AND source = ?", (repo, source)
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO norms (id, repo, statement, scope, support,
                    distinct_reviewers, confidence, evidence_prs, source, quote, evidence,
                    clustered_by, named_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        n.id, repo, n.statement, n.scope, n.support, n.distinct_reviewers,
                        n.confidence, json_col(list(n.evidence_prs)), n.source, n.quote,
                        json_col([list(e) for e in n.evidence]), n.clustered_by, n.named_by,
                    )
                    for n in norms
                ],
            )

    def norms(self, repo: str) -> list[Norm]:
        """This repository's norms, declared first, then by confidence. Never another's."""
        rows = self.db.query(
            "SELECT * FROM norms WHERE repo = ?"
            " ORDER BY source = 'mined', confidence DESC, support DESC, id",
            (repo,),
        )
        return [_norm_from(r) for r in rows]

    def norm(self, repo: str, norm_id: str) -> Norm | None:
        row = self.db.one("SELECT * FROM norms WHERE repo = ? AND id = ?", (repo, norm_id))
        return _norm_from(row) if row else None


def _charter_dict(charter: Charter) -> dict[str, Any]:
    return {
        "repo": charter.repo, "sha": charter.sha, "name": charter.name,
        "summary": charter.summary,
        "claims": [
            {"kind": c.kind, "text": c.text, "source": c.source} for c in charter.claims
        ],
        "version": charter.version, "license": charter.license,
        "toolchain": charter.toolchain, "entry_points": list(charter.entry_points),
        "modules": [list(m) for m in charter.modules],
        "public_api": list(charter.public_api), "dependencies": list(charter.dependencies),
        "standards": list(charter.standards),
    }


def _charter_from(data: dict[str, Any], row: Any) -> Charter:
    return Charter(
        repo=str(data.get("repo") or row["repo"]),
        sha=str(data.get("sha") or row["sha"]),
        name=str(data.get("name", "")),
        summary=str(data.get("summary", "")),
        claims=tuple(
            Claim(kind=c["kind"], text=c["text"], source=c["source"])
            for c in data.get("claims", [])
        ),
        version=str(data.get("version", "")),
        license=str(data.get("license", "")),
        toolchain=str(data.get("toolchain", "")),
        entry_points=tuple(data.get("entry_points", [])),
        modules=tuple(
            (str(m[0]), str(m[1]), str(m[2])) for m in data.get("modules", []) if len(m) == 3
        ),
        public_api=tuple(data.get("public_api", [])),
        dependencies=tuple(data.get("dependencies", [])),
        standards=tuple(data.get("standards", [])),
        built_at=float(row["built_at"]),
        number=int(row["number"]),
    )


def _comment_key(comment: dict[str, Any]) -> str:
    """GitHub's comment id when there is one; otherwise a digest of what identifies it."""
    if comment.get("comment_id"):
        return str(comment["comment_id"])
    import hashlib

    basis = f"{comment['pr_number']}|{comment['created_at']}|{comment['body']}"
    return "h" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]  # noqa: S324 - an id, not security


def _norm_from(row: Any) -> Norm:
    return Norm(
        id=row["id"], statement=row["statement"], scope=row["scope"],
        support=int(row["support"]), distinct_reviewers=int(row["distinct_reviewers"]),
        confidence=float(row["confidence"]),
        evidence_prs=tuple(int(n) for n in _loads(row["evidence_prs"], [])),
        source=row["source"], quote=row["quote"],
        evidence=tuple(
            (int(e[0]), str(e[1]), str(e[2])) for e in _loads(row["evidence"], [])
            if isinstance(e, list) and len(e) == 3
        ),
        clustered_by=row["clustered_by"], named_by=row["named_by"],
    )
