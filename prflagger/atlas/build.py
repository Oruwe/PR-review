"""Assembling the repo picture.

The Atlas is what the first view shows: what this repository *is*, before any
pull request is considered. Languages, modules and their sizes, what imports
what, where the churn is, which modules have no tests, and the public surface.

It is a plain dict by the time it leaves here, because it goes straight into
SQLite and then to the browser. Building it touches no model and no network —
only the checkout and `git`.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import structlog

from prflagger.analysis.callgraph import build_call_graph, index_symbols
from prflagger.atlas.cartography import Churn, FileFact, churn_by_path, inventory, module_of
from prflagger.core.models import Symbol
from prflagger.lang.base import Toolchain

__all__ = ["build_atlas"]

log = structlog.get_logger(__name__)

#: How many modules the dependency graph draws. Beyond this the picture is a
#: hairball and tells the reader nothing, so the largest are kept.
_MAX_GRAPH_NODES = 60


def build_atlas(
    repo_path: Path,
    *,
    slug: str,
    sha: str,
    toolchain: Toolchain,
    package_roots: tuple[str, ...] = (),
    churn_days: int = 365,
    max_files: int = 25_000,
    on_structure: Callable[[dict[str, Symbol], dict[str, set[str]]], None] | None = None,
) -> dict[str, Any]:
    """Everything the Atlas view needs, as JSON-ready data.

    `on_structure` receives the symbol index and call graph before they are
    aggregated away, so a caller can persist them without paying to compute them
    twice. `max_files` bounds the symbol pass: past it the repository still gets
    sizes, churn and topology, and `analysis_depth` says the structural layer was
    skipped rather than the view implying a depth it never reached.
    """
    started = time.monotonic()
    files = inventory(repo_path, test_globs=toolchain.test_globs)
    churn = churn_by_path(repo_path, days=churn_days)

    skipped_reason = ""
    source_files = sum(1 for f in files if f.language not in ("Markdown", "Config", "Other"))
    if source_files > max_files:
        symbols: dict[str, Symbol] = {}
        call_edges: dict[str, set[str]] = {}
        skipped_reason = (
            f"{source_files:,} source files exceeds the {max_files:,}-file budget for "
            f"symbol extraction; sizes, churn and test topology are still measured"
        )
        log.warning("atlas.structure_skipped", repo=slug, files=source_files)
    else:
        symbols, call_edges = _structure(repo_path, toolchain, package_roots)
        if on_structure is not None and symbols:
            on_structure(symbols, call_edges)
    modules = _modules(files, churn, symbols)
    edges = _module_edges(call_edges, symbols)
    test_map = _test_topology(files, modules)

    languages: dict[str, int] = defaultdict(int)
    for fact in files:
        languages[fact.language] += fact.loc

    atlas = {
        "repo": slug,
        "sha": sha,
        "built_at": time.time(),
        "build_seconds": round(time.monotonic() - started, 2),
        "toolchain": toolchain.as_dict(),
        "totals": {
            "files": len(files),
            "loc": sum(f.loc for f in files),
            "test_files": sum(1 for f in files if f.is_test),
            "symbols": len(symbols),
            "call_edges": sum(len(v) for v in call_edges.values()),
            "contributors": len({a for c in churn.values() for a in c.authors}),
            "commits_window": sum(c.commits for c in churn.values()),
            "churn_days": churn_days,
        },
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "modules": modules,
        "edges": edges,
        "hotspots": _hotspots(modules),
        "surface": _surface(symbols),
        "test_topology": test_map,
        "analysis_depth": "structural" if symbols else "surface",
        "depth_note": skipped_reason,
    }
    log.info(
        "atlas.built", repo=slug, sha=sha[:12], modules=len(modules),
        symbols=len(symbols), seconds=atlas["build_seconds"],
    )
    return atlas


# ----------------------------------------------------------------------------------
# Structure
# ----------------------------------------------------------------------------------


def _structure(
    repo_path: Path, toolchain: Toolchain, package_roots: tuple[str, ...]
) -> tuple[dict[str, Symbol], dict[str, set[str]]]:
    """Symbols and call edges, where the pack supports extracting them.

    Only Python has an exact extractor today. A repo in any other language still
    gets a full Atlas — sizes, churn, hotspots, test topology — and the
    `analysis_depth` field says plainly that the symbol layer is absent, rather
    than the view implying a depth that was never reached.
    """
    if toolchain.grammar != "python":
        return {}, {}

    roots = [repo_path / r for r in package_roots if (repo_path / r).is_dir()]
    if not roots:
        roots = _infer_roots(repo_path)

    # One analysis root, not one per package. Resolving a call means finding the
    # callee among the indexed symbols, so a graph built separately per package
    # can never see an edge that crosses packages — which on a repository with
    # forty top-level packages is every edge worth drawing.
    root = _common_root(roots, repo_path)
    symbols: dict[str, Symbol] = {}
    edges: dict[str, set[str]] = {}
    try:
        # Repo-relative, or module attribution falls into a `/tmp/...` bucket and
        # every real module reports zero symbols.
        symbols = {
            fqn: replace(symbol, file=_relative(symbol.file, repo_path))
            for fqn, symbol in index_symbols(root).items()
        }
        edges = build_call_graph(root)
    except (OSError, RecursionError, ValueError) as error:
        log.warning("atlas.structure_failed", root=str(root), error=str(error)[:200])
    return symbols, edges


def _common_root(roots: list[Path], repo_path: Path) -> Path:
    """The shallowest directory containing every package root.

    A single root keeps fully-qualified names consistent across packages, which
    is what lets a cross-package call resolve at all.
    """
    if len(roots) == 1:
        return roots[0]
    try:
        common = Path(os.path.commonpath([str(r) for r in roots]))
    except ValueError:
        return repo_path
    # Never climb above the repository itself.
    return common if common.is_relative_to(repo_path) or common == repo_path else repo_path


def _relative(path: str, repo_path: Path) -> str:
    """Repo-relative posix, so a path means the same thing on any machine."""
    try:
        return Path(path).relative_to(repo_path).as_posix()
    except ValueError:
        return path


def _infer_roots(repo_path: Path) -> list[Path]:
    """Top-level importable packages, so a repo works without configuration."""
    found: list[Path] = []
    for parent in (repo_path / "src", repo_path):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if (
                child.is_dir()
                and (child / "__init__.py").is_file()
                and child.name not in ("tests", "test", "docs")
            ):
                found.append(child)
        if found:
            break
    return found or [repo_path]


# ----------------------------------------------------------------------------------
# Modules
# ----------------------------------------------------------------------------------


def _modules(
    files: list[FileFact], churn: dict[str, Churn], symbols: dict[str, Symbol]
) -> list[dict[str, Any]]:
    """One row per module: size, churn, symbol count, test presence."""
    grouped: dict[str, dict[str, Any]] = {}
    for fact in files:
        name = module_of(fact.path)
        entry = grouped.setdefault(
            name,
            {
                "name": name, "loc": 0, "files": 0, "test_files": 0, "symbols": 0,
                "commits": 0, "authors": set(), "languages": defaultdict(int),
                "paths": [],
            },
        )
        entry["loc"] += fact.loc
        entry["files"] += 1
        entry["test_files"] += 1 if fact.is_test else 0
        entry["languages"][fact.language] += fact.loc
        entry["paths"].append(fact.path)
        measured = churn.get(fact.path)
        if measured:
            entry["commits"] += measured.commits
            entry["authors"].update(measured.authors)

    by_file: dict[str, int] = defaultdict(int)
    for symbol in symbols.values():
        by_file[module_of(symbol.file)] += 1
    for name, entry in grouped.items():
        entry["symbols"] = by_file.get(name, 0)

    out: list[dict[str, Any]] = []
    for entry in grouped.values():
        entry["authors"] = len(entry["authors"])
        entry["languages"] = dict(sorted(entry["languages"].items(), key=lambda kv: -kv[1]))
        entry["paths"] = sorted(entry["paths"])[:200]
        out.append(entry)
    return sorted(out, key=lambda m: -int(m["loc"]))


def _module_edges(
    call_edges: dict[str, set[str]], symbols: dict[str, Symbol]
) -> list[dict[str, Any]]:
    """Call edges lifted to module level, with a weight per pair."""
    module_for = {fqn: module_of(symbol.file) for fqn, symbol in symbols.items()}
    weights: dict[tuple[str, str], int] = defaultdict(int)
    for caller, callees in call_edges.items():
        source = module_for.get(caller)
        if source is None:
            continue
        for callee in callees:
            target = module_for.get(callee)
            if target is None or target == source:
                continue  # a module calling itself is not information
            weights[(source, target)] += 1

    ranked = sorted(weights.items(), key=lambda kv: -kv[1])
    keep = {m for pair, _ in ranked[:_MAX_GRAPH_NODES * 4] for m in pair}
    return [
        {"source": s, "target": t, "weight": w}
        for (s, t), w in ranked
        if s in keep and t in keep
    ][: _MAX_GRAPH_NODES * 4]


def _hotspots(modules: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    """Where change concentrates in code that tests do not cover.

    score = normalised(churn) x normalised(size) x (1 - test ratio)

    The three factors are reported alongside the score so a reader can disagree
    with the weighting and still use the numbers. This is a prioritisation hint,
    not a judgement about quality.
    """
    if not modules:
        return []
    widest_churn = max((m["commits"] for m in modules), default=0) or 1
    widest_loc = max((m["loc"] for m in modules), default=0) or 1

    scored = []
    for module in modules:
        if module["files"] == 0:
            continue
        churn_factor = module["commits"] / widest_churn
        size_factor = module["loc"] / widest_loc
        untested = 1.0 - (module["test_files"] / module["files"])
        score = churn_factor * size_factor * untested
        if score <= 0:
            continue
        scored.append(
            {
                "name": module["name"],
                "score": round(score, 4),
                "churn_factor": round(churn_factor, 3),
                "size_factor": round(size_factor, 3),
                "untested_factor": round(untested, 3),
                "commits": module["commits"],
                "loc": module["loc"],
                "authors": module["authors"],
            }
        )
    return sorted(scored, key=lambda h: -h["score"])[:limit]


def _surface(symbols: dict[str, Symbol], limit: int = 400) -> list[dict[str, Any]]:
    """Public symbols — nothing whose name, or whose parent, starts with `_`."""
    public = [
        symbol
        for fqn, symbol in symbols.items()
        if not any(part.startswith("_") for part in fqn.split(".")[1:])
    ]
    public.sort(key=lambda s: s.fqn)
    return [
        {"fqn": s.fqn, "kind": s.kind, "file": s.file, "line": s.line_start}
        for s in public[:limit]
    ]


def _test_topology(files: list[FileFact], modules: list[dict[str, Any]]) -> dict[str, Any]:
    """Which modules carry tests, and which carry none.

    Presence of a test file is not coverage and this does not claim it is — it
    is the cheap structural signal, and the coverage probe supplies the real
    number per run.
    """
    untested = [m["name"] for m in modules if m["test_files"] == 0 and m["symbols"] > 0]
    return {
        "modules_with_tests": sum(1 for m in modules if m["test_files"] > 0),
        "modules_total": len(modules),
        "untested_modules": sorted(untested)[:40],
        "test_files": sum(1 for f in files if f.is_test),
    }
