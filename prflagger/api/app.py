"""The web surface: four views, a small REST API, and one WebSocket.

Server-rendered shells with vanilla ES modules on top. No build step, no CDN,
nothing to install before the page works — which matters because this is meant
to be run by one person on one machine, and a toolchain that rots is a service
that stops starting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from prflagger.api.service import Service
from prflagger.api.ws import stream_events
from prflagger.atlas.build import build_atlas
from prflagger.core.config import Config, cache_root
from prflagger.core.errors import RepoUnavailable
from prflagger.core.models import Repo, RunState
from prflagger.engine.states import ORDER, progress_of
from prflagger.lang.detect import for_repo
from prflagger.probes.differential import SEVERITY
from prflagger.vcs.worktrees import worktree_for

__all__ = ["create_app"]

log = structlog.get_logger(__name__)

_WEB = Path(__file__).resolve().parent.parent / "web"

#: Finding kinds ranked by what they mean, not by colour. The UI pairs each with
#: an icon and a label, so status never carries meaning on its own.
KIND_STATUS = {
    "behavior_change": "critical",
    "timeout": "critical",
    "oom": "critical",
    "collection_error": "critical",
    "api_change": "serious",
    "test_removed": "serious",
    "install_failed": "serious",
    "coverage_gap": "warning",
    "lint_regression": "warning",
}

KIND_LABEL = {
    "behavior_change": "Behaviour change",
    "test_removed": "Test removed",
    "api_change": "API change",
    "coverage_gap": "Coverage gap",
    "lint_regression": "Lint regression",
    "timeout": "Timeout",
    "oom": "Out of memory",
    "install_failed": "Install failed",
    "collection_error": "Collection error",
}


def create_app(
    service: Service | None = None,
    *,
    config: Config | None = None,
    watch: bool = True,
) -> FastAPI:
    """Build the application. `service` is injectable so tests drive a real one."""
    built = service or Service.build(config)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await built.start(watch=watch)
        try:
            yield
        finally:
            await built.stop()

    app = FastAPI(title="PR Flagger", version="2.0", lifespan=lifespan)
    app.state.service = built
    templates = Jinja2Templates(directory=str(_WEB / "templates"))
    templates.env.globals.update(
        KIND_STATUS=KIND_STATUS, KIND_LABEL=KIND_LABEL, SEVERITY=SEVERITY,
    )
    app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")

    # -- views ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Any:
        repos = []
        for repo in built.store.repos():
            pulls = built.store.pulls(repo.slug, state="open")
            runs = built.store.runs(repo=repo.slug, limit=200)
            repos.append(
                {
                    "repo": repo,
                    "open_pulls": len(pulls),
                    "runs": len(runs),
                    "flagged": sum(
                        1 for r in runs
                        if r.state is RunState.DONE and built.store.observations(r.id)
                    ),
                    "has_atlas": bool(repo.atlas_sha),
                }
            )
        return templates.TemplateResponse(
            request, "index.html", {"repos": repos, "health": built.health()}
        )

    @app.get("/repo/{owner}/{name}", response_class=HTMLResponse)
    async def atlas_view(request: Request, owner: str, name: str) -> Any:
        slug = f"{owner}/{name}"
        repo = built.store.repo(slug)
        if repo is None:
            raise HTTPException(404, f"{slug} is not being watched")
        atlas = built.store.atlas(slug)
        return templates.TemplateResponse(
            request, "atlas.html",
            {"repo": repo, "atlas": atlas, "pulls": built.store.pulls(slug)},
        )

    @app.get("/repo/{owner}/{name}/prs", response_class=HTMLResponse)
    async def queue_view(request: Request, owner: str, name: str) -> Any:
        slug = f"{owner}/{name}"
        repo = built.store.repo(slug)
        if repo is None:
            raise HTTPException(404, f"{slug} is not being watched")
        return templates.TemplateResponse(
            request, "queue.html",
            {"repo": repo, "rows": _queue_rows(built, slug), "kinds": sorted(KIND_LABEL)},
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_view(request: Request, run_id: str) -> Any:
        run = built.store.run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        return templates.TemplateResponse(
            request, "run.html",
            {
                "run": run,
                "pull": built.store.pull(run.repo, run.pr_number),
                "jobs": built.store.jobs(run_id),
                "stages": [s.value for s in ORDER],
                "cursor": 0,
                "progress": progress_of(run.state),
            },
        )

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    async def report_view(request: Request, run_id: str) -> Any:
        run = built.store.run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        return templates.TemplateResponse(
            request, "report.html", _report_context(built, run_id)
        )

    # -- REST ----------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> Any:
        return built.health()

    @app.get("/api/repos")
    async def list_repos() -> Any:
        return [
            {
                "slug": r.slug, "default_branch": r.default_branch,
                "toolchain": r.toolchain_id, "atlas_sha": r.atlas_sha,
                "package_roots": list(r.package_roots),
            }
            for r in built.store.repos()
        ]

    @app.post("/api/repos")
    async def add_repo(payload: dict[str, Any]) -> Any:
        slug = str(payload.get("slug", "")).strip().strip("/")
        if "/" not in slug:
            raise HTTPException(400, "slug must be owner/name")
        entry = built.config.repo(slug)
        try:
            branch = (
                entry.default_branch
                if entry.clone_url
                else built.github.default_branch(slug)
            )
        except RepoUnavailable as error:
            raise HTTPException(400, str(error)) from error
        repo = built.store.put_repo(
            Repo(
                slug=slug, default_branch=branch,
                package_roots=tuple(entry.package_roots), added_at=time.time(),
            )
        )
        built.bus.emit("repo.added", repo=slug)
        return {"slug": repo.slug, "default_branch": repo.default_branch}

    @app.post("/api/repos/{owner}/{name}/atlas")
    async def build_repo_atlas(
        owner: str, name: str, payload: dict[str, Any] | None = None
    ) -> Any:
        slug = f"{owner}/{name}"
        repo = built.store.repo(slug)
        if repo is None:
            raise HTTPException(404, f"{slug} is not being watched")
        entry = built.config.repo(slug)
        sha = str((payload or {}).get("sha") or "") or repo.default_branch
        try:
            tree = await asyncio.to_thread(
                worktree_for, slug, sha, url=entry.clone_url or None
            )
        except RepoUnavailable as error:
            raise HTTPException(400, str(error)) from error
        toolchain = await asyncio.to_thread(
            for_repo, tree, config=built.config, slug=slug
        )
        def persist(symbols: dict[str, Any], edges: dict[str, set[str]]) -> None:
            # Ranking reads caller counts from here; without this step centrality
            # is a constant and the ordering ignores how connected a symbol is.
            built.store.put_symbols(slug, list(symbols.values()))
            built.store.put_call_edges(slug, edges)

        atlas = await asyncio.to_thread(
            build_atlas, tree, slug=slug, sha=sha, toolchain=toolchain,
            package_roots=tuple(entry.package_roots), on_structure=persist,
        )
        built.store.put_atlas(slug, sha, atlas)
        built.bus.emit("atlas.built", repo=slug, sha=sha, modules=len(atlas["modules"]))
        return {"repo": slug, "sha": sha, "modules": len(atlas["modules"])}

    @app.get("/api/repos/{owner}/{name}/atlas")
    async def get_atlas(owner: str, name: str) -> Any:
        atlas = built.store.atlas(f"{owner}/{name}")
        if atlas is None:
            raise HTTPException(404, "no atlas built for this repo yet")
        return atlas

    @app.get("/api/repos/{owner}/{name}/pulls")
    async def get_pulls(owner: str, name: str) -> Any:
        return _queue_rows(built, f"{owner}/{name}")

    @app.post("/api/repos/{owner}/{name}/poll")
    async def poll_now(owner: str, name: str) -> Any:
        entry = built.config.repo(f"{owner}/{name}")
        queued = await built.watcher._poll_repo(f"{owner}/{name}", entry.max_prs)  # noqa: SLF001
        return {"queued": queued}

    @app.post("/api/runs")
    async def trigger_run(payload: dict[str, Any]) -> Any:
        slug = str(payload.get("repo", ""))
        number = int(payload.get("pr") or 0)
        pull = built.store.pull(slug, number)
        if pull is None:
            raise HTTPException(404, f"{slug}#{number} is not known; poll the repo first")
        run_id = built.scheduler.submit(
            pull, trigger="manual", force=bool(payload.get("force"))
        )
        if run_id is None:
            existing = built.store.latest_run(slug, number)
            return {"run_id": existing.id if existing else None, "queued": False}
        return {"run_id": run_id, "queued": True}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> Any:
        built.pool.cancel(run_id)
        return {"cancelled": built.scheduler.cancel(run_id)}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> Any:
        run = built.store.run(run_id)
        if run is None:
            raise HTTPException(404, f"no run {run_id}")
        return {
            "id": run.id, "repo": run.repo, "pr": run.pr_number,
            "state": run.state.value, "progress": progress_of(run.state),
            "base_sha": run.base_sha, "head_sha": run.head_sha,
            "error": run.error, "created_at": run.created_at,
            "started_at": run.started_at, "finished_at": run.finished_at,
            "jobs": built.store.jobs(run_id),
        }

    @app.get("/api/runs/{run_id}/report")
    async def get_report(run_id: str) -> Any:
        if built.store.run(run_id) is None:
            raise HTTPException(404, f"no run {run_id}")
        return _report_payload(built, run_id)

    @app.get("/api/runs/{run_id}/log")
    async def get_log(run_id: str, job: str | None = None) -> Any:
        """The raw transcript, for download. Plain text, in order."""
        directory = cache_root() / "runs" / run_id
        if not directory.is_dir():
            raise HTTPException(404, "no transcript for this run")
        lines: list[str] = []
        for path in sorted(directory.glob("*.ndjson")):
            if job and job not in path.stem:
                continue
            lines.append(f"===== {path.stem} =====")
            with contextlib.suppress(OSError):
                for raw in path.read_text(encoding="utf-8").splitlines():
                    with contextlib.suppress(json.JSONDecodeError):
                        entry = json.loads(raw)
                        lines.append(
                            f"[{entry['offset_ms']:>8}ms {entry['stream']:<6}] {entry['text']}"
                        )
        return PlainTextResponse("\n".join(lines))

    @app.get("/api/budget")
    async def budget() -> Any:
        spent = float(built.db.scalar("SELECT SUM(usd) FROM llm_spend", default=0.0) or 0.0)
        caps = built.config.budget
        return {
            "spent_usd": round(spent, 4),
            "total_usd": caps.total_usd,
            "remaining_usd": round(max(0.0, caps.total_usd - spent), 4),
            "per_run_usd": caps.per_run_usd,
            "per_repo_daily_usd": caps.per_repo_daily_usd,
            "calls": int(built.db.scalar("SELECT COUNT(*) FROM llm_spend", default=0) or 0),
        }

    # -- sockets --------------------------------------------------------------

    @app.websocket("/ws/runs/{run_id}")
    async def run_socket(
        websocket: WebSocket, run_id: str, cursor: int = 0, lines: int = 0
    ) -> None:
        await stream_events(
            websocket, built.bus, run_id=run_id, cursor=cursor, lines=lines
        )

    @app.websocket("/ws/repo/{owner}/{name}")
    async def repo_socket(
        websocket: WebSocket, owner: str, name: str, cursor: int = 0
    ) -> None:
        await stream_events(websocket, built.bus, repo=f"{owner}/{name}", cursor=cursor)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> Any:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc)}, status_code=404)
        return templates.TemplateResponse(
            request, "404.html", {"detail": str(exc)}, status_code=404
        )

    return app


# ----------------------------------------------------------------------------------
# Shaping data for the views
# ----------------------------------------------------------------------------------


def _queue_rows(service: Service, slug: str) -> list[dict[str, Any]]:
    """PRs with their latest run, ranked by what the reader should look at first.

    Risk is the sum of the run's observation scores, not a count: one behaviour
    change matters more than six lint nits, and a ranking by count says otherwise.
    """
    rows = []
    for pull in service.store.pulls(slug, state="open"):
        run = service.store.latest_run(slug, pull.number)
        observations = service.store.observations(run.id) if run else []
        by_kind: dict[str, int] = {}
        for observation in observations:
            by_kind[observation.kind] = by_kind.get(observation.kind, 0) + 1
        rows.append(
            {
                "number": pull.number,
                "title": pull.title,
                "author": pull.author,
                "draft": pull.draft,
                "updated_at": pull.updated_at,
                "additions": pull.additions,
                "deletions": pull.deletions,
                "head_sha": pull.head_sha,
                "run_id": run.id if run else None,
                "state": run.state.value if run else "not run",
                "progress": progress_of(run.state) if run else 0.0,
                "findings": len(observations),
                "by_kind": by_kind,
                "worst": max((o.severity for o in observations), default=0.0),
                "risk": round(sum(o.rank_score for o in observations), 3),
            }
        )
    return sorted(rows, key=lambda r: (-r["risk"], -r["findings"], -r["number"]))


def _report_payload(service: Service, run_id: str) -> dict[str, Any]:
    run = service.store.run(run_id)
    assert run is not None  # noqa: S101 - callers check first
    observations = service.store.observations(run_id)
    adjudications = service.store.adjudications(run_id)
    suggestions = service.store.suggestions(run_id)

    coverage: dict[str, Any] = {"verified": [], "skipped": [], "complete": False}
    for event in service.bus.since(0, run_id=run_id):
        if event.type == "run.observations" and isinstance(event.payload.get("coverage"), dict):
            coverage = event.payload["coverage"]

    findings = []
    for observation in observations:
        adjudication = adjudications.get(observation.id)
        suggestion = suggestions.get(observation.id)
        findings.append(
            {
                "id": observation.id,
                "kind": observation.kind,
                "label": KIND_LABEL.get(observation.kind, observation.kind),
                "status": KIND_STATUS.get(observation.kind, "warning"),
                "symbol": observation.symbol,
                "what_changed": observation.what_changed,
                "how_we_know": observation.how_we_know,
                "evidence_ref": observation.evidence_ref,
                "severity": observation.severity,
                "confidence": observation.confidence,
                "rank_score": observation.rank_score,
                "adjudication": (
                    {
                        "assessment": adjudication.assessment,
                        "reasoning": adjudication.reasoning,
                        "citations": [c.__dict__ for c in adjudication.citations],
                        "model": adjudication.model,
                    }
                    if adjudication
                    else None
                ),
                "suggestion": (
                    {
                        "summary": suggestion.summary,
                        "rationale": suggestion.rationale,
                        "patch_sketch": suggestion.patch_sketch,
                        "confidence": suggestion.confidence,
                        "citations": [c.__dict__ for c in suggestion.citations],
                    }
                    if suggestion
                    else None
                ),
            }
        )

    pull = service.store.pull(run.repo, run.pr_number)
    return {
        "run": {
            "id": run.id, "repo": run.repo, "pr": run.pr_number,
            "state": run.state.value, "base_sha": run.base_sha, "head_sha": run.head_sha,
            "created_at": run.created_at, "finished_at": run.finished_at,
            "duration_s": (
                round((run.finished_at or 0) - (run.started_at or 0), 1)
                if run.finished_at and run.started_at
                else None
            ),
            "usd_spent": run.usd_spent,
        },
        "pull": (
            {"number": pull.number, "title": pull.title, "author": pull.author,
             "body": pull.body}
            if pull
            else None
        ),
        "findings": findings,
        "coverage": coverage,
        "jobs": service.store.jobs(run_id),
        "counts": _counts(findings),
    }


def _report_context(service: Service, run_id: str) -> dict[str, Any]:
    payload = _report_payload(service, run_id)
    payload["payload_json"] = json.dumps(payload)
    return payload


def _counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for finding in findings:
        out[finding["kind"]] = out.get(finding["kind"], 0) + 1
    return out
