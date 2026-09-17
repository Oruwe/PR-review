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
import sys
import tomllib
from pathlib import Path
from typing import Any

import structlog

from prflagger.analysis.blast import changed_symbols
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
from prflagger.report.rank import rank
from prflagger.report.render import render
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

    norms = sub.add_parser("norms", help="print learned norms with evidence")
    norms.add_argument("--repo", required=True, help="owner/name")

    args = parser.parse_args(argv)
    _configure_logging()

    if args.command == "brain":
        return _brain_build(args.repo, args.limit)
    if args.command == "check":
        return _check(args.repo, args.base, args.head, args.out)
    return _norms(args.repo)


# ----------------------------------------------------------------------------------


def _check(repo: Path, base: str, head: str, out: Path) -> int:
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

    print(f"{len(ordered)} observation(s) -> {out}")
    for finding in ordered[:10]:
        print(f"  [{finding.kind}] {finding.symbol}")
        print(f"      {finding.what_changed}")
    if skipped:
        print("\nNot verified:")
        for row in skipped:
            print(f"  {row['module']}: {row['reason']}")
    return 0


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
