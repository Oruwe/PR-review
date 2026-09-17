"""Blast radius: what a diff could have changed the behaviour of.

We do not test the entire PR — that is intractable. We characterize the symbols the diff
touches plus their transitive callers, because a change in `c()` can only surface through
something that calls it.
"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from prflagger.analysis.callgraph import build_call_graph, index_symbols
from prflagger.analysis.diff import changed_ranges
from prflagger.analysis.symbols import module_fqn_for, symbols_in_file
from prflagger.models import Symbol

__all__ = ["blast_radius", "changed_symbols", "package_root_for"]


def blast_radius(repo: Path, base: str, head: str, *, hops: int = 2) -> list[Symbol]:
    """Symbols overlapping changed ranges, plus their transitive callers up to `hops`."""
    package_root = package_root_for(repo)
    symbols = index_symbols(package_root)
    touched = changed_symbols(repo, base, head, package_root=package_root)

    callers_of: dict[str, set[str]] = defaultdict(set)
    for caller, callees in build_call_graph(package_root).items():
        for callee in callees:
            callers_of[callee].add(caller)

    reached = {symbol.fqn for symbol in touched}
    frontier = set(reached)
    for _ in range(max(0, hops)):
        nxt = {caller for fqn in frontier for caller in callers_of.get(fqn, ())} - reached
        if not nxt:
            break
        reached |= nxt
        frontier = nxt

    out = {symbol.fqn: symbol for symbol in touched}
    for fqn in reached:
        if fqn not in out and fqn in symbols:
            out[fqn] = _repo_relative(symbols[fqn], repo)
    return [out[fqn] for fqn in sorted(out)]


def changed_symbols(
    repo: Path, base: str, head: str, *, package_root: Path | None = None
) -> list[Symbol]:
    """Symbols whose line range overlaps a changed range in the head revision."""
    root = package_root or package_root_for(repo)
    touched: list[Symbol] = []
    for relative, spans in changed_ranges(repo, base, head).items():
        if not relative.endswith(".py"):
            continue
        path = repo / relative
        if not path.is_file() or not _within(path, root):
            continue
        for symbol in symbols_in_file(path, module_fqn_for(path, root)):
            if _overlaps(symbol, spans):
                touched.append(replace(symbol, file=relative))
    return touched


def _overlaps(symbol: Symbol, spans: list[tuple[int, int]]) -> bool:
    return any(
        symbol.line_start <= end and start <= symbol.line_end for start, end in spans
    )


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _repo_relative(symbol: Symbol, repo: Path) -> Symbol:
    try:
        relative = Path(symbol.file).resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return symbol
    return replace(symbol, file=relative)


def package_root_for(repo: Path) -> Path:
    """The importable package under analysis.

    Prefers what config.toml declares; otherwise infers a single top-level package,
    so a synthetic fixture repo works without configuration.
    """
    declared = _declared_package_root()
    if declared is not None and (repo / declared).is_dir():
        return repo / declared

    for parent in (repo / "src", repo):
        if not parent.is_dir():
            continue
        packages = sorted(
            child
            for child in parent.iterdir()
            if child.is_dir() and (child / "__init__.py").is_file() and child.name != "tests"
        )
        if len(packages) == 1:
            return packages[0]
        if packages:
            return parent
    return repo


def _declared_package_root() -> str | None:
    config = Path("config.toml")
    if not config.is_file():
        return None
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    raw = data.get("target", {}).get("package_root")
    return str(raw) if raw else None
