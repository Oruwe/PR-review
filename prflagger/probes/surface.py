"""Structural probes: the public API surface, and the repo's own linters.

Neither uses a model. The API surface comes from parsing both commits; the lint
delta comes from running the repo's *own* configured linters at both commits and
subtracting. A diagnostic that already existed before the change is not this
change's problem, and reporting it as one is how a tool teaches people to ignore
it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import structlog

from prflagger.analysis.callgraph import index_symbols
from prflagger.core.ids import observation_id
from prflagger.core.models import Observation, Symbol
from prflagger.lang.base import Toolchain, parse_lint
from prflagger.probes.differential import SEVERITY

__all__ = ["lint_observations", "public_index", "surface_observations"]

log = structlog.get_logger(__name__)

#: Beyond this many new diagnostics the list stops being a finding and starts
#: being a wall; the count is reported instead of every individual line.
_MAX_LINT_REPORTED = 25


def _public(fqn: str) -> bool:
    """Public means no underscore-prefixed component after the top-level package."""
    return not any(part.startswith("_") for part in fqn.split(".")[1:])


def _roots(tree: Path, package_roots: tuple[str, ...]) -> list[Path]:
    found = [tree / r for r in package_roots if (tree / r).is_dir()]
    if found:
        return found
    for parent in (tree / "src", tree):
        if not parent.is_dir():
            continue
        packages = [
            child
            for child in sorted(parent.iterdir())
            if child.is_dir()
            and (child / "__init__.py").is_file()
            and child.name not in ("tests", "test", "docs")
        ]
        if packages:
            return packages
    return [tree]


def public_index(tree: Path, package_roots: tuple[str, ...] = ()) -> dict[str, Symbol]:
    """Public symbols at one commit, repo-relative. Shared by the surface probe and
    the charter, so a run parses each commit once rather than twice."""
    try:
        return _index(tree, package_roots)
    except (OSError, RecursionError) as error:
        log.warning("surface.unavailable", error=str(error)[:200])
        return {}


def surface_observations(
    run_id: str,
    base_tree: Path,
    head_tree: Path,
    package_roots: tuple[str, ...] = (),
    *,
    base_index: dict[str, Symbol] | None = None,
    head_index: dict[str, Symbol] | None = None,
) -> list[Observation]:
    """Public symbols added, removed, or moved between the two commits."""
    base = base_index if base_index is not None else public_index(base_tree, package_roots)
    head = head_index if head_index is not None else public_index(head_tree, package_roots)
    if not base and not head:
        return []

    observations: list[Observation] = []

    for fqn in sorted(set(base) - set(head)):
        observations.append(
            _observation(
                run_id, "api_change", fqn,
                what_changed=f"public symbol {fqn} was removed",
                how_we_know=(
                    f"present at base in {base[fqn].file}:{base[fqn].line_start}, "
                    f"absent at head"
                ),
                evidence_ref=f"{base[fqn].file}:{base[fqn].line_start}",
                confidence=0.9,
            )
        )

    for fqn in sorted(set(head) - set(base)):
        symbol = head[fqn]
        observations.append(
            _observation(
                run_id, "api_change", fqn,
                what_changed=f"public symbol {fqn} was added",
                how_we_know=(
                    f"absent at base, defined at head in {symbol.file}:{symbol.line_start} "
                    f"as a {symbol.kind}"
                ),
                evidence_ref=f"{symbol.file}:{symbol.line_start}",
                confidence=0.85,
            )
        )

    for fqn in sorted(set(base) & set(head)):
        if base[fqn].kind != head[fqn].kind:
            observations.append(
                _observation(
                    run_id, "api_change", fqn,
                    what_changed=(
                        f"public symbol {fqn} changed from {base[fqn].kind} to "
                        f"{head[fqn].kind}"
                    ),
                    how_we_know=(
                        f"base {base[fqn].file}:{base[fqn].line_start} is a "
                        f"{base[fqn].kind}; head {head[fqn].file}:{head[fqn].line_start} "
                        f"is a {head[fqn].kind}"
                    ),
                    evidence_ref=f"{head[fqn].file}:{head[fqn].line_start}",
                    confidence=0.8,
                )
            )
    return observations


def _index(tree: Path, package_roots: tuple[str, ...]) -> dict[str, Symbol]:
    symbols: dict[str, Symbol] = {}
    for root in _roots(tree, package_roots):
        for fqn, symbol in index_symbols(root).items():
            if _public(fqn):
                symbols[fqn] = replace(symbol, file=_relative(symbol.file, tree))
    return symbols


def _relative(path: str, tree: Path) -> str:
    """Repo-relative, so a citation resolves against the checkout.

    Symbols are indexed from a worktree under the cache directory; leaving
    that prefix in place would put a path in the report that means nothing on
    anyone else's machine.
    """
    try:
        return Path(path).relative_to(tree).as_posix()
    except ValueError:
        return path


def lint_observations(
    run_id: str,
    toolchain: Toolchain,
    base_output: dict[str, str],
    head_output: dict[str, str],
) -> list[Observation]:
    """Diagnostics present at head that were not present at base."""
    observations: list[Observation] = []
    for lint in toolchain.lints:
        before = {
            (_strip(path), code, message)
            for path, code, message in parse_lint(lint.parser, base_output.get(lint.parser, ""))
        }
        after = [
            (_strip(path), code, message)
            for path, code, message in parse_lint(lint.parser, head_output.get(lint.parser, ""))
        ]
        introduced = [entry for entry in after if entry not in before]
        if not introduced:
            continue

        for path, code, message in introduced[:_MAX_LINT_REPORTED]:
            observations.append(
                _observation(
                    run_id, "lint_regression", path or "(repo)",
                    what_changed=f"{lint.tool} reports {code} at head that it did not at base",
                    how_we_know=f"{lint.tool} {code} in {path}: {message[:200]}",
                    evidence_ref=f"{path}:{code}",
                    confidence=0.85,
                )
            )
        if len(introduced) > _MAX_LINT_REPORTED:
            observations.append(
                _observation(
                    run_id, "lint_regression", "(repo)",
                    what_changed=(
                        f"{lint.tool} reports {len(introduced)} new diagnostics at head; "
                        f"the first {_MAX_LINT_REPORTED} are listed individually"
                    ),
                    how_we_know=(
                        f"{len(before)} diagnostics at base, {len(after)} at head, "
                        f"run with the repo's own {lint.tool} configuration"
                    ),
                    evidence_ref=f"{lint.tool}:count",
                    confidence=0.9,
                )
            )
    return observations


def _strip(path: str) -> str:
    """Drop the container's mount prefix so base and head paths compare equal."""
    return path.removeprefix("/src/").removeprefix("/src").lstrip("/")


def _observation(
    run_id: str,
    kind: str,
    symbol: str,
    *,
    what_changed: str,
    how_we_know: str,
    evidence_ref: str,
    confidence: float,
) -> Observation:
    return Observation(
        id=observation_id(run_id, kind, symbol, evidence_ref),
        run_id=run_id,
        kind=kind,
        symbol=symbol,
        what_changed=what_changed,
        how_we_know=how_we_know,
        evidence_ref=evidence_ref,
        severity=SEVERITY.get(kind, 0.5),
        confidence=confidence,
    )
