"""The web surface: four views, a small REST API, and one WebSocket.

Server-rendered shells with vanilla ES modules on top. No build step, no CDN,
nothing to install before the page works — which matters because this is meant
to be run by one person on one machine, and a toolchain that rots is a service
that stops starting.
"""

from __future__ import annotations

import contextlib
import functools
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from prflagger.api.auth import Tokens, install, websocket_role
from prflagger.api.service import Service
from prflagger.api.ws import stream_events
from prflagger.core.config import Config, cache_root
from prflagger.core.errors import RepoUnavailable
from prflagger.core.models import Norm, Repo, RunState
from prflagger.engine.states import ORDER, progress_of
from prflagger.probes.differential import SEVERITY

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

#: Relevance is shown as a status: core findings draw the eye, peripheral ones do not.
RELEVANCE_STATUS = {"core": "critical", "supporting": "warning", "peripheral": "neutral"}

#: How loud each drift level is on screen. Minor is history, not an alert.
LEVEL_STATUS = {"major": "critical", "notable": "serious", "minor": "neutral", "none": "good"}

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
    tokens: Tokens | None = None,
) -> FastAPI:
    """Build the application. `service` is injectable so tests drive a real one;
    `tokens` defaults to the environment's (see `api.auth`)."""
    built = service or Service.build(config)
    tokens = tokens if tokens is not None else Tokens.from_env()

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
        RELEVANCE_STATUS=RELEVANCE_STATUS, LEVEL_STATUS=LEVEL_STATUS,
        # Read per request: a major update stays on every page until someone
        # acknowledges it, which is what makes it louder than a finding.
        open_notices=lambda: built.store.notifications(
            open_only=True, min_level="major", limit=5
        ),
    )
    templates.env.globals.update(
        # Viewers see every page, and none of the controls that change anything.
        # The middleware refuses those requests regardless; hiding them is courtesy.
        can_write=lambda request: getattr(request.state, "role", "admin") == "admin",
        signed_in_as=lambda request: (
            getattr(request.state, "role", None) if getattr(request.state, "auth", False)
            else None
        ),
    )
    app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")

    def render_login(
        request: Request, *, next: str, error: str, status_code: int = 200  # noqa: A002
    ) -> Any:
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": error}, status_code=status_code
        )

    install(app, tokens, render_login)

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
            {
                "repo": repo, "atlas": atlas, "pulls": built.store.pulls(slug),
                "charter": built.store.charter(slug),
                "changes": built.store.charter_changes(slug, limit=3),
                "brain": _brain_payload(built, slug),
            },
        )

    @app.get("/repo/{owner}/{name}/changes", response_class=HTMLResponse)
    async def changes_view(request: Request, owner: str, name: str) -> Any:
        slug = f"{owner}/{name}"
        repo = built.store.repo(slug)
        if repo is None:
            raise HTTPException(404, f"{slug} is not being watched")
        return templates.TemplateResponse(
            request, "changes.html",
            {
                "repo": repo,
                "charter": built.store.charter(slug),
                "history": built.store.charter_history(slug),
                "changes": built.store.charter_changes(slug),
                "notices": built.store.notifications(repo=slug, limit=50),
                "webhook": built.notifier.webhook_configured,
            },
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
        return {
            **built.health(),
            "auth": "tokens" if tokens.enabled else "open (loopback only)",
        }

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
        built.remember(slug)
        return {"slug": repo.slug, "default_branch": repo.default_branch}

    @app.post("/api/repos/{owner}/{name}/atlas")
    async def build_repo_atlas(
        owner: str, name: str, payload: dict[str, Any] | None = None
    ) -> Any:
        """Rebuild the repository's memory: its atlas and its charter, together."""
        slug = f"{owner}/{name}"
        if built.store.repo(slug) is None:
            raise HTTPException(404, f"{slug} is not being watched")
        sha = str((payload or {}).get("sha") or "") or None
        refreshed = await built.keeper.refresh(slug, sha, reason="requested", force=True)
        if refreshed is None:
            raise HTTPException(400, f"could not read {slug}; see the service log")
        return {
            "repo": slug,
            "sha": refreshed.charter.sha,
            "modules": refreshed.atlas_modules,
            "charter": refreshed.charter.number,
            "level": refreshed.drift.level if refreshed.drift else "baseline",
        }

    @app.get("/api/repos/{owner}/{name}/charter")
    async def get_charter(owner: str, name: str) -> Any:
        slug = f"{owner}/{name}"
        charter = built.store.charter(slug)
        if charter is None:
            raise HTTPException(404, "no charter built for this repo yet")
        return {
            "repo": charter.repo, "sha": charter.sha, "number": charter.number,
            "name": charter.name, "summary": charter.summary, "version": charter.version,
            "license": charter.license, "toolchain": charter.toolchain,
            "claims": [c.__dict__ for c in charter.claims],
            "entry_points": list(charter.entry_points),
            "modules": [list(m) for m in charter.modules],
            "public_api_count": len(charter.public_api),
            "dependencies": list(charter.dependencies),
            "standards": list(charter.standards),
            "history": built.store.charter_history(slug),
        }

    @app.get("/api/repos/{owner}/{name}/norms")
    async def get_norms(owner: str, name: str) -> Any:
        slug = f"{owner}/{name}"
        if built.store.repo(slug) is None:
            raise HTTPException(404, f"{slug} is not being watched")
        return _brain_payload(built, slug)

    @app.post("/api/repos/{owner}/{name}/norms")
    async def relearn(owner: str, name: str) -> Any:
        """Read new review history now and mine the norms again."""
        slug = f"{owner}/{name}"
        if built.store.repo(slug) is None:
            raise HTTPException(404, f"{slug} is not being watched")
        learned = await built.brain.refresh(slug, reason="requested", force=True)
        if learned is None:
            raise HTTPException(502, "the review history could not be read; see the log")
        return learned.__dict__

    @app.get("/api/repos/{owner}/{name}/changes")
    async def get_changes(owner: str, name: str) -> Any:
        return built.store.charter_changes(f"{owner}/{name}")

    @app.get("/api/notifications")
    async def list_notifications(
        open_only: bool = False, level: str = "notable", repo: str | None = None
    ) -> Any:
        if level not in LEVEL_STATUS:
            raise HTTPException(400, f"level must be one of {sorted(LEVEL_STATUS)}")
        return [
            {**n.__dict__, "evidence": list(n.evidence)}
            for n in built.store.notifications(repo=repo, open_only=open_only, min_level=level)
        ]

    @app.post("/api/notifications/{notification_id}/ack")
    async def acknowledge(notification_id: str) -> Any:
        acknowledged = built.store.acknowledge(notification_id)
        if acknowledged:
            built.bus.emit("notification.acknowledged", notification_id=notification_id)
        return {"acknowledged": acknowledged}

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
        ledger = built.models.ledger
        spent = ledger.spent()
        caps = built.config.budget
        return {
            "spent_usd": round(spent, 4),
            "total_usd": caps.total_usd,
            "remaining_usd": round(max(0.0, caps.total_usd - spent), 4),
            "per_run_usd": caps.per_run_usd,
            "per_repo_daily_usd": caps.per_repo_daily_usd,
            "today_usd": round(ledger.spent(since=ledger.day_start()), 4),
            "models_available": built.models.available,
            "models_unavailable_reason": built.models.unavailable_reason,
            **ledger.summary(),
        }

    # -- sockets --------------------------------------------------------------

    @app.websocket("/ws/runs/{run_id}")
    async def run_socket(
        websocket: WebSocket, run_id: str, cursor: int = 0, lines: int = 0
    ) -> None:
        if websocket_role(tokens, websocket) is None:
            await websocket.close(code=4401)
            return
        await stream_events(
            websocket, built.bus, run_id=run_id, cursor=cursor, lines=lines
        )

    @app.websocket("/ws/repo/{owner}/{name}")
    async def repo_socket(
        websocket: WebSocket, owner: str, name: str, cursor: int = 0
    ) -> None:
        if websocket_role(tokens, websocket) is None:
            await websocket.close(code=4401)
            return
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
                "charter_impact": service.store.run_charter_impact(run.id) if run else "",
                "risk": round(sum(o.rank_score for o in observations), 3),
            }
        )
    return sorted(rows, key=lambda r: (-r["risk"], -r["findings"], -r["number"]))


_CODE_REF = re.compile(r"^(?:(base|head):)?(.+?):(\d+)(?:-(\d+))?$")


def _citation_view(
    citation: Any, *, slug: str, base_sha: str, head_sha: str, pr: int,
    on_github: bool, norms: dict[str, Norm],
) -> dict[str, Any]:
    """A citation as the report shows it: what it says, and where to check it."""
    view = {"type": citation.type, "ref": citation.ref, "quote": citation.quote,
            "url": "", "label": ""}
    if citation.type == "norm":
        norm = norms.get(citation.ref)
        view["label"] = norm.statement if norm else ""
        anchor = f"norm-{citation.ref}" if norm and norm.source == "mined" else "norms"
        view["url"] = f"/repo/{slug}#{anchor}"
    elif citation.type == "code" and on_github:
        match = _CODE_REF.match(citation.ref)
        if match:
            side, path, first, last = match.groups()
            sha = base_sha if side == "base" else head_sha
            lines = f"L{first}-L{last}" if last else f"L{first}"
            view["url"] = f"https://github.com/{slug}/blob/{sha}/{path}#{lines}"
    elif citation.type == "diff" and on_github and pr:
        view["url"] = f"https://github.com/{slug}/pull/{pr}/files"
    return view


def _norm_dict(norm: Norm) -> dict[str, Any]:
    return {
        "id": norm.id, "statement": norm.statement, "source": norm.source,
        "support": norm.support, "distinct_reviewers": norm.distinct_reviewers,
        "confidence": norm.confidence, "evidence_prs": list(norm.evidence_prs),
        "quote": norm.quote,
        "evidence": [
            {"pr": pr, "url": url, "where": where} for pr, url, where in norm.evidence
        ],
        "clustered_by": norm.clustered_by, "named_by": norm.named_by,
    }


def _brain_payload(service: Service, slug: str) -> dict[str, Any]:
    """What this repository holds itself to, and how the service knows."""
    seen, kept = service.store.review_counts(slug)
    state = service.store.brain_state(slug) or {}
    norms = service.store.norms(slug)
    return {
        "repo": slug,
        "declared": [_norm_dict(n) for n in norms if n.source == "declared"],
        "mined": [_norm_dict(n) for n in norms if n.source == "mined"],
        "comments_seen": seen,
        "comments_enforced": kept,
        "retention": round(kept / seen, 3) if seen else None,
        "pulls_read": int(state.get("prs_seen") or 0),
        "built_at": state.get("built_at") or None,
        "clustered_by": state.get("clustered_by", ""),
        "named_by": state.get("named_by", ""),
        "note": state.get("note", ""),
        "on_github": service.brain.on_github(slug),
    }


def _report_payload(service: Service, run_id: str) -> dict[str, Any]:
    run = service.store.run(run_id)
    assert run is not None  # noqa: S101 - callers check first
    observations = service.store.observations(run_id)
    adjudications = service.store.adjudications(run_id)
    suggestions = service.store.suggestions(run_id)

    coverage: dict[str, Any] = {"verified": [], "skipped": [], "complete": False}
    impact: dict[str, Any] = {"level": "", "signals": []}
    for event in service.bus.since(0, run_id=run_id):
        if event.type == "run.observations" and isinstance(event.payload.get("coverage"), dict):
            coverage = event.payload["coverage"]
        if event.type == "run.charter_impact":
            impact = {
                "level": event.payload.get("level", ""),
                "signals": event.payload.get("signals", []),
            }

    norms = {n.id: n for n in service.store.norms(run.repo)}
    cite = functools.partial(
        _citation_view, slug=run.repo, base_sha=run.base_sha, head_sha=run.head_sha,
        pr=run.pr_number, on_github=service.brain.on_github(run.repo), norms=norms,
    )
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
                "relevance": observation.relevance,
                "relevance_note": observation.relevance_note,
                "norm": _norm_dict(norm) if (norm := norms.get(observation.norm_id)) else None,
                "adjudication": (
                    {
                        "assessment": adjudication.assessment,
                        "reasoning": adjudication.reasoning,
                        "citations": [cite(c) for c in adjudication.citations],
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
                        "citations": [cite(c) for c in suggestion.citations],
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
        "charter_impact": impact,
        "charter": (
            {"name": charter.name, "summary": charter.summary, "number": charter.number,
             "sha": charter.sha}
            if (charter := service.store.charter(run.repo)) else None
        ),
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
