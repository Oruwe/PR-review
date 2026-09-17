"""Command line entry points.

    prflagger brain build --repo <slug>
    prflagger check --repo <path> --base <sha> --head <sha> --out report.html
    prflagger norms --repo <slug>

This module is the only place that prints.
"""

from __future__ import annotations

import argparse
import json
import logging
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
    check.add_argument(
        "--json",
        dest="json_out",
        type=Path,
        default=Path(".cache/ui/findings.json"),
        help="findings document the interface renders from",
    )
    check.add_argument(
        "--ui",
        type=Path,
        default=Path("index.html"),
        help="the interface: one self-contained HTML file",
    )

    norms = sub.add_parser("norms", help="print learned norms with evidence")
    norms.add_argument("--repo", required=True, help="owner/name")

    ui = sub.add_parser("ui", help="render the interface from a findings document")
    ui.add_argument("--data", type=Path, default=Path(".cache/ui/findings.json"))
    ui.add_argument("--out", type=Path, default=Path("index.html"))

    args = parser.parse_args(argv)
    _configure_logging()

    if args.command == "brain":
        return _brain_build(args.repo, args.limit)
    if args.command == "check":
        return _check(args.repo, args.base, args.head, args.out, args.json_out, args.ui)
    if args.command == "ui":
        return _ui(args.data, args.out)
    return _norms(args.repo)


# ----------------------------------------------------------------------------------


def _check(
    repo: Path, base: str, head: str, out: Path, json_out: Path, ui_out: Path
) -> int:
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
