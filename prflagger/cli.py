"""Command line entry points.

    prflagger serve [--host H] [--port P] [--no-watch]
    prflagger watch --repo <slug>
    prflagger brain build --repo <slug>
    prflagger check --repo <path> --base <sha> --head <sha> --out report.html
    prflagger norms --repo <slug>
    prflagger gc [--days N]
    prflagger llm check [--model ID]
    prflagger backup [--out FILE]
    prflagger restore FILE [--with-config]

This module is the only place that prints.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import structlog

from prflagger.analysis.blast import blast_radius, changed_symbols
from prflagger.brain.enforce import enforced_comments
from prflagger.brain.harvest import GhUnavailable, harvest, prs_path
from prflagger.brain.norms import (
    build_profile,
    cluster_norms,
    declarative_norms,
    profile_path,
    write_profile,
)
from prflagger.brain.store import SqliteGraphStore
from prflagger.models import Finding, Norm
from prflagger.probes import api_diff, coverage_delta, lint_regression
from prflagger.report.payload import PipelineNode, PrRecord, build_payload, write_payload
from prflagger.report.rank import rank
from prflagger.report.render import render
from prflagger.report.web import render_app
from prflagger.sandbox.runner import cache_root

__all__ = ["main"]

log = structlog.get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prflagger", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    brain = sub.add_parser("brain", help="build the repo brain")
    brain_sub = brain.add_subparsers(dest="brain_command", required=True)
    build = brain_sub.add_parser("build", help="harvest, filter, cluster, store")
    build.add_argument("--repo", required=True, help="owner/name")
    build.add_argument("--limit", type=int, default=150)

    check = sub.add_parser("check", help="verify a change and write a report")
    check.add_argument("--repo", required=True, type=Path, help="path to a checkout")
    check.add_argument("--base", required=True)
    check.add_argument("--head", required=True)
    check.add_argument("--out", type=Path, default=Path("report.html"))
    # Both default to siblings of --out. A command's outputs belong where it was told
    # to write, not in the working directory: anything else makes a check run somewhere
    # else overwrite them.
    check.add_argument(
        "--json",
        dest="json_out",
        type=Path,
        default=None,
        help="findings document the interface renders from (default: <out>.findings.json)",
    )
    check.add_argument(
        "--ui",
        type=Path,
        default=None,
        help="the interface, one self-contained HTML file (default: <out dir>/index.html)",
    )

    norms = sub.add_parser("norms", help="print learned norms with evidence")
    norms.add_argument("--repo", required=True, help="owner/name")

    ui = sub.add_parser("ui", help="render the interface from a findings document")
    ui.add_argument("--data", type=Path, default=Path(".cache/ui/findings.json"))
    ui.add_argument("--out", type=Path, default=Path("index.html"))

    serve = sub.add_parser("serve", help="run the service and its web interface")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument(
        "--no-watch", action="store_true", help="do not poll GitHub; run only on request"
    )
    serve.add_argument("--reload", action="store_true", help="reload on code changes")

    watch = sub.add_parser("watch", help="start watching a repository")
    watch.add_argument("--repo", required=True, help="owner/name")

    collect = sub.add_parser("gc", help="reclaim worktrees, images, transcripts and events")
    collect.add_argument("--days", type=int, default=0, help="keep this many days")
    collect.add_argument(
        "--dry-run", action="store_true", help="report what would go, remove nothing"
    )

    llm = sub.add_parser("llm", help="the model provider")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    llm_check = llm_sub.add_parser(
        "check", help="make one tiny real call to prove credentials, region and model work"
    )
    llm_check.add_argument("--model", default="", help="model id (default: [models] light)")

    saving = sub.add_parser("backup", help="snapshot the database (safe while serving)")
    saving.add_argument("--out", type=Path, default=None,
                        help="archive to write (default: prflagger-backup-<time>.tar.gz)")
    restoring = sub.add_parser("restore", help="put a backup back; the service must be stopped")
    restoring.add_argument("archive", type=Path)
    restoring.add_argument("--with-config", action="store_true",
                           help="also restore config.toml from the backup")

    args = parser.parse_args(argv)
    _configure_logging()

    if args.command == "brain":
        return _brain_build(args.repo, args.limit)
    if args.command == "check":
        return _check(args.repo, args.base, args.head, args.out, args.json_out, args.ui)
    if args.command == "ui":
        return _ui(args.data, args.out)
    if args.command == "serve":
        return _serve(args.host, args.port, watch=not args.no_watch, reload=args.reload)
    if args.command == "watch":
        return _watch(args.repo)
    if args.command == "gc":
        return _gc(args.days, dry_run=args.dry_run)
    if args.command == "llm":
        return _llm_check(args.model)
    if args.command == "backup":
        return _backup(args.out)
    if args.command == "restore":
        return _restore(args.archive, with_config=args.with_config)
    return _norms(args.repo)


# ----------------------------------------------------------------------------------
# serve / watch / gc
# ----------------------------------------------------------------------------------


_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def _serve(host: str, port: int, *, watch: bool, reload: bool) -> int:
    """Run the always-on service. This is the main entry point for v2."""
    import uvicorn

    from prflagger.api.app import create_app
    from prflagger.api.auth import ADMIN_ENV, Tokens
    from prflagger.api.service import Service
    from prflagger.core.config import load

    config = load()
    bind_host = host or config.server.host
    bind_port = port or config.server.port

    try:
        tokens = Tokens.from_env()
    except ValueError as error:
        print(f"refusing to start: {error}", file=sys.stderr)
        return 2
    if not tokens.enabled and bind_host not in _LOOPBACK:
        # Anyone who can reach an open service can add repositories, start runs in
        # its sandbox and spend its model budget. Loopback is the only safe place
        # for that; anything else needs a token.
        print(
            f"refusing to listen on {bind_host} without a sign-in token.\n"
            f"  set {ADMIN_ENV} (e.g. `openssl rand -hex 32`), or listen on 127.0.0.1 "
            "behind an SSH tunnel or VPN.",
            file=sys.stderr,
        )
        return 2

    service = Service.build(config)
    for entry in config.repos:
        if service.store.repo(entry.slug) is None:
            from prflagger.core.models import Repo

            service.store.put_repo(
                Repo(
                    slug=entry.slug,
                    default_branch=entry.default_branch,
                    package_roots=tuple(entry.package_roots),
                    added_at=time.time(),
                )
            )

    print(f"PR Flagger on http://{bind_host}:{bind_port}")
    print(f"  watching {len(config.repos)} configured repo(s); "
          f"{service.pool.capacity} sandbox slot(s)")
    if not service.github.authenticated:
        print("  note: no GitHub token found — public repos only, 60 calls/hour")
    if not watch:
        print("  polling disabled; runs start only when you ask for them")
    if tokens.enabled:
        print("  sign-in: admin" + (" and viewer tokens" if tokens.viewer else " token"))
    else:
        print("  sign-in: none (listening on loopback only)")

    app = create_app(service, config=config, watch=watch, tokens=tokens)
    marker = _mark_running(service.db.path)
    try:
        uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")
    finally:
        marker.unlink(missing_ok=True)
    return 0


def _mark_running(db_path: Path) -> Path:
    """Record this process as the one using `db_path`, so a restore can refuse."""
    import socket

    from prflagger.storage.backup import pid_file

    marker = pid_file(db_path)
    marker.write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "since": time.time()}),
        encoding="utf-8",
    )
    return marker


def _watch(slug: str) -> int:
    """Record a repository so the service starts polling it."""
    from prflagger.api.service import Service
    from prflagger.core.models import Repo

    service = Service.build()
    entry = service.config.repo(slug)
    try:
        branch = (
            entry.default_branch if entry.clone_url else service.github.default_branch(slug)
        )
    except Exception as error:  # noqa: BLE001 - report, do not traceback
        print(f"could not reach {slug}: {_reason(error)}", file=sys.stderr)
        return 1
    service.store.put_repo(
        Repo(
            slug=slug, default_branch=branch,
            package_roots=tuple(entry.package_roots), added_at=time.time(),
        )
    )
    print(f"watching {slug} (default branch {branch})")
    return 0


def _config_path() -> Path:
    from prflagger.core.config import CONFIG_ENV

    return Path(os.environ.get(CONFIG_ENV) or "config.toml")


def _backup(out: Path | None) -> int:
    from prflagger.storage.backup import backup
    from prflagger.storage.db import default_path

    target = out or Path(time.strftime("prflagger-backup-%Y%m%d-%H%M%S.tar.gz"))
    try:
        manifest = backup(default_path(), target, config=_config_path())
    except FileNotFoundError as error:
        print(f"nothing to back up: {error}", file=sys.stderr)
        return 1
    rows = manifest["rows"]
    print(f"backup      : {target}")
    print(f"runs        : {rows.get('runs', 0)}   charters: {rows.get('charters', 0)}"
          f"   norms: {rows.get('norms', 0)}"
          f"   review comments: {rows.get('review_comments', 0)}")
    included = manifest["includes_config"]
    print(f"config      : {'included' if included else 'not found, not included'}")
    print("not included: " + ", ".join(manifest["not_included"]) + " (rebuilt on demand)")
    return 0


def _restore(archive: Path, *, with_config: bool) -> int:
    from prflagger.storage.backup import ServiceRunning, restore
    from prflagger.storage.db import default_path

    try:
        manifest = restore(archive, default_path(),
                           config=_config_path() if with_config else None)
    except ServiceRunning as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"could not restore: {error}", file=sys.stderr)
        return 1
    print(f"restored    : {archive} (taken {manifest['created_at']})")
    if manifest.get("previous_database"):
        print(f"kept        : the database it replaced, at {manifest['previous_database']}")
    print("next        : start the service; it migrates the schema if needed")
    return 0


def _llm_check(model: str) -> int:
    """One real call, uncached, through the same client and ledger the service uses."""
    from prflagger.core.config import load
    from prflagger.llm.client import build_client
    from prflagger.llm.ledger import BudgetExceeded
    from prflagger.llm.provider import Request
    from prflagger.storage.db import connect

    config = load()
    client = build_client(config, connect())
    chosen = model or config.models.light
    spent = client.ledger.spent()
    print(f"credentials : {client.credentials or 'none'}")
    print(f"region      : {config.models.region}")
    print(f"models      : light={config.models.light}  heavy={config.models.heavy}")
    print(f"budget      : ${spent:.4f} spent of ${config.budget.total_usd:.2f}"
          f" (per run ${config.budget.per_run_usd:.2f},"
          f" per repo per day ${config.budget.per_repo_daily_usd:.2f})")
    if not client.available:
        print(f"unavailable : {client.unavailable_reason}")
        return 1
    started = time.monotonic()
    try:
        answer = client.ask(
            Request(model=chosen, prompt="Reply with the single word: ready", max_tokens=8),
            stage="check", use_cache=False,
        )
    except BudgetExceeded as error:
        print(f"refused     : {error}")
        return 1
    except Exception as error:  # noqa: BLE001 - the point is to show what went wrong
        print(f"failed      : {type(error).__name__}: {str(error)[:300]}")
        if client.unavailable_reason:
            print(f"now         : {client.unavailable_reason}")
        return 1
    usage = answer.completion
    print(f"reply       : {answer.text.strip()!r} from {chosen}"
          f" in {time.monotonic() - started:.1f}s")
    print(f"usage       : {usage.input_tokens} in, {usage.output_tokens} out"
          f" — ${answer.usd:.6f}, recorded in the ledger")
    print("ready       : adjudication and norm naming will use this provider")
    return 0


def _gc(days: int, *, dry_run: bool = False) -> int:
    """Reclaim disk. Worktrees, images, transcripts and cached job results."""
    from prflagger.api.service import Service
    from prflagger.engine.janitor import disk_free_ratio, sweep

    service = Service.build()
    before = disk_free_ratio()
    reclaimed = sweep(
        service.store, service.config, service.bus,
        keep_days=days or None, dry_run=dry_run,
    )
    if not dry_run:
        service.db.execute("VACUUM")

    print(f"worktrees removed   : {reclaimed.worktrees}")
    print(f"images removed      : {reclaimed.images}")
    print(f"transcripts removed : {reclaimed.transcripts}")
    print(f"events pruned       : {reclaimed.events}")
    print(f"reclaimed           : {reclaimed.bytes_freed / (1024 * 1024):.1f} MB")
    print(f"disk free           : {before * 100:.1f}% -> {disk_free_ratio() * 100:.1f}%")
    for failure in reclaimed.failures:
        print(f"  could not remove: {failure}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------------------


def _check(
    repo: Path, base: str, head: str, out: Path, json_out: Path | None, ui_out: Path | None
) -> int:
    json_out = json_out or out.with_suffix(".findings.json")
    ui_out = ui_out or out.with_name("index.html")
    repo = repo.resolve()
    slug = _configured_slug()
    verified: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    findings: list[Finding] = []

    for name, probe in (
        ("api surface", api_diff),
        ("coverage", coverage_delta),
        ("lint and types", lint_regression),
    ):
        try:
            produced = probe(repo, base, head)
        except Exception as error:  # noqa: BLE001 - a probe failing is a coverage fact
            skipped.append({"module": name, "reason": _reason(error)})
            continue
        findings.extend(produced)
        verified.append(
            {"module": name, "detail": f"{len(produced)} observation(s)"}
        )

    # Behavioural verification is the expensive half and the first to be unavailable.
    try:
        from prflagger.characterize.differential import differential

        behavioural = differential(repo, base, head)
        findings.extend(behavioural)
        verified.append(
            {
                "module": "behavioural differential",
                "detail": f"{len(behavioural)} observation(s)",
            }
        )
    except Exception as error:  # noqa: BLE001
        skipped.append({"module": "behavioural differential", "reason": _reason(error)})

    modules = sorted({symbol.file for symbol in changed_symbols(repo, base, head)})
    for module in modules:
        verified.append({"module": module, "detail": "changed and analysed"})

    store = SqliteGraphStore(cache_root() / "brain" / "store.sqlite")
    ordered = rank(findings, store.norms_for(slug), store)

    profile = _profile_for(slug, repo)
    profile["coverage"] = {"verified": verified, "skipped": skipped}
    render(ordered, profile, out)

    # The interface renders from this document and nothing else, so it is written even
    # when a probe was unavailable: a partial run still has to be inspectable.
    payload = build_payload(
        ordered,
        profile,
        prs=[_pr_record(repo, base, head, ordered)],
        nodes=_pipeline_state(ordered, verified, skipped),
    )
    write_payload(payload, json_out)
    render_app(payload, ui_out)

    print(f"{len(ordered)} observation(s) -> {out}")
    print(f"findings document -> {json_out}")
    print(f"interface -> {ui_out}")
    for finding in ordered[:10]:
        print(f"  [{finding.kind}] {finding.symbol}")
        print(f"      {finding.what_changed}")
    if skipped:
        print("\nNot verified:")
        for row in skipped:
            print(f"  {row['module']}: {row['reason']}")
    return 0


def _ui(data: Path, out: Path) -> int:
    """Render the interface from a findings document written by an earlier check."""
    if not data.is_file():
        print(f"no findings document at {data}", file=sys.stderr)
        print("run `prflagger check --json <path>` first", file=sys.stderr)
        return 1
    try:
        payload = json.loads(data.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"{data} is not valid JSON: {error}", file=sys.stderr)
        return 1
    render_app(payload, out)
    print(f"interface -> {out}")
    return 0


def _pr_record(repo: Path, base: str, head: str, findings: list[Finding]) -> PrRecord:
    """The change under analysis, described by git rather than by prose."""
    subject = _git(repo, "log", "-1", "--format=%s", head)
    number = 0
    if "(#" in subject:
        candidate = subject.rsplit("(#", 1)[-1].rstrip(")")
        number = int(candidate) if candidate.isdigit() else 0
    files: list[dict[str, Any]] = []
    insertions = deletions = 0
    for line in _git(repo, "diff", "--numstat", base, head).splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0
        removed = int(parts[1]) if parts[1].isdigit() else 0
        insertions += added
        deletions += removed
        files.append({"path": parts[2], "insertions": added, "deletions": removed})
    touched = changed_symbols(repo, base, head)
    radius = blast_radius(repo, base, head)
    fqns = {symbol.fqn for symbol in touched}
    return PrRecord(
        number=number,
        title=subject.rsplit(" (#", 1)[0] if "(#" in subject else subject,
        author=_git(repo, "log", "-1", "--format=%an", head),
        branch=_git(repo, "rev-parse", "--abbrev-ref", "HEAD") or head[:10],
        base=_git(repo, "rev-parse", base) or base,
        head=_git(repo, "rev-parse", head) or head,
        merged_at=_git(repo, "log", "-1", "--format=%aI", head),
        url="",
        files_changed=tuple(sorted(files, key=lambda row: -row["insertions"])),
        changed_symbols=tuple(touched),
        callers=tuple(symbol for symbol in radius if symbol.fqn not in fqns),
        insertions=insertions,
        deletions=deletions,
        finding_ids=tuple(f"f{index}" for index in range(len(findings))),
        analysed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def _pipeline_state(
    findings: list[Finding],
    verified: list[dict[str, str]],
    skipped: list[dict[str, str]],
) -> list[PipelineNode]:
    """Each node's state, read off what this run actually managed to do."""
    by_kind: dict[str, int] = {}
    for finding in findings:
        by_kind[finding.kind] = by_kind.get(finding.kind, 0) + 1
    ran = {row["module"] for row in verified}
    failed = {row["module"]: row["reason"] for row in skipped}
    probes = sum(
        by_kind.get(kind, 0)
        for kind in ("api_change", "coverage_gap", "lint_regression")
    )
    behavioural = "behavioural differential"
    return [
        PipelineNode("pr", "complete", "base and head resolved"),
        PipelineNode("c2", "complete", "changed symbols plus callers"),
        PipelineNode(
            "c3",
            "complete" if behavioural in ran else "failed",
            failed.get(behavioural, "characterization tests generated"),
        ),
        PipelineNode(
            "c1base",
            "complete" if behavioural in ran else "idle",
            "candidates executed against base",
        ),
        PipelineNode(
            "filter",
            "complete" if behavioural in ran else "idle",
            "tests that fail on base are discarded",
        ),
        PipelineNode(
            "c1head",
            "complete" if behavioural in ran else "idle",
            "survivors executed against head",
        ),
        PipelineNode(
            "c4",
            "complete" if behavioural in ran else "failed",
            f"{by_kind.get('behavior_change', 0)} behaviour change(s)",
            count=by_kind.get("behavior_change"),
        ),
        PipelineNode("c5", "idle", "no harvest in this run"),
        PipelineNode("c6", "idle", "no harvest to filter"),
        PipelineNode("c7", "idle", "no clusters in this run"),
        PipelineNode("c8", "complete", "norms read from the store"),
        PipelineNode(
            "c9",
            "complete" if probes else "idle",
            f"{probes} observation(s) from the probes",
            count=probes or None,
        ),
        PipelineNode(
            "c10",
            "complete",
            f"{len(findings)} finding(s) ranked",
            count=len(findings),
        ),
    ]


def _git(repo: Path, *argv: str) -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def _brain_build(slug: str, limit: int) -> int:
    try:
        path = harvest(slug, limit=limit)
    except GhUnavailable as error:
        print(f"harvest unavailable: {error}", file=sys.stderr)
        print("falling back to a declarative-only profile", file=sys.stderr)
        path = prs_path(slug)

    comments = enforced_comments(path) if path.is_file() else []
    mined: list[Norm] = []
    if comments:
        try:
            mined = cluster_norms(comments)
        except Exception as error:  # noqa: BLE001
            print(f"clustering unavailable: {_reason(error)}", file=sys.stderr)

    repo_path = _checkout_for(slug)
    norms = mined + declarative_norms(repo_path)

    store = SqliteGraphStore(cache_root() / "brain" / "store.sqlite")
    store.put_norms(slug, norms)

    profile = build_profile(slug, repo_path, norms, prs_analyzed=len(comments))
    written = write_profile(profile, profile_path(slug, cache_root()))
    print(f"{len(norms)} norm(s) ({len(mined)} mined, {len(norms) - len(mined)} declared)")
    print(f"profile -> {written}")
    return 0


def _norms(slug: str) -> int:
    store = SqliteGraphStore(cache_root() / "brain" / "store.sqlite")
    norms = store.norms_for(slug) or declarative_norms(_checkout_for(slug))
    if not norms:
        print(f"no norms for {slug}; run `prflagger brain build --repo {slug}`")
        return 0
    for norm in norms:
        evidence = (
            ", ".join(f"#{number}" for number in norm.evidence_prs)
            or "declared by the repository's own configuration"
        )
        print(f"{norm.id}")
        print(f"  {norm.statement}")
        print(
            f"  support {norm.support} - {norm.distinct_reviewers} reviewer(s)"
            f" - confidence {norm.confidence:.2f} - {norm.scope}"
        )
        print(f"  {evidence}")
    return 0


# ----------------------------------------------------------------------------------


def _reason(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return f"{error.__class__.__name__}: {text[:200]}"


def _configured_slug() -> str:
    config = Path("config.toml")
    if not config.is_file():
        return ""
    try:
        return str(
            tomllib.loads(config.read_text(encoding="utf-8"))["target"]["slug"]
        )
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return ""


def _checkout_for(slug: str) -> Path:
    """A worktree of the target, for reading its declared configuration."""
    root = cache_root() / "worktrees" / slug.replace("/", "__")
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir():
                return child
    return Path.cwd()


def _profile_for(slug: str, repo: Path) -> dict[str, Any]:
    path = profile_path(slug, cache_root())
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
    return build_profile(slug, repo, declarative_norms(repo), prs_analyzed=0)


def _configure_logging() -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
    )


if __name__ == "__main__":
    raise SystemExit(main())
