"""Public API surface, diffed with `ast`.

Private behaviour is allowed to change and flagging it produces noise, so anything with
a leading underscore anywhere in its path is out of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import structlog

from prflagger.analysis.blast import package_root_for
from prflagger.analysis.symbols import module_fqn_for
from prflagger.models import Finding
from prflagger.probes._support import SEVERITY, files_at, norm_for, read_blob

__all__ = ["api_diff", "public_surface"]

log = structlog.get_logger(__name__)


def api_diff(repo: Path, base: str, head: str) -> list[Finding]:
    """AST diff of public symbols (no leading underscore). Findings for added, removed,
    and signature-changed."""
    norm = norm_for("api_change", repo)
    if norm is None:
        log.info("api_diff.no_declared_norm", repo=str(repo))
        return []

    before = public_surface(repo, base)
    after = public_surface(repo, head)

    findings: list[Finding] = []
    for fqn in sorted(set(after) - set(before)):
        findings.append(
            _finding(fqn, f"added to the public API as {after[fqn]}", norm, "added")
        )
    for fqn in sorted(set(before) - set(after)):
        findings.append(
            _finding(fqn, f"removed from the public API (was {before[fqn]})", norm, "removed")
        )
    for fqn in sorted(set(before) & set(after)):
        if before[fqn] != after[fqn]:
            findings.append(
                _finding(
                    fqn,
                    f"public signature changed from {before[fqn]} to {after[fqn]}",
                    norm,
                    "signature",
                )
            )
    log.info("api_diff", findings=len(findings), base=base[:8], head=head[:8])
    return findings


def _finding(fqn: str, what: str, norm: object, change: str) -> Finding:
    from prflagger.models import Norm

    assert isinstance(norm, Norm)
    return Finding(
        kind="api_change",
        symbol=fqn,
        what_changed=f"{fqn} {what}",
        how_we_know=f"ast diff of public symbols: {change}",
        norm=norm,
        # An ast diff is exact; there is nothing probabilistic about it.
        confidence=1.0,
        severity=SEVERITY["api_change"],
    )


def public_surface(repo: Path, revision: str) -> dict[str, str]:
    """fqn -> signature, for every public definition at `revision`."""
    package_root = package_root_for(repo)
    try:
        relative_root = package_root.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return {}

    surface: dict[str, str] = {}
    for relative in files_at(repo, revision, relative_root):
        source = read_blob(repo, revision, relative)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        module = module_fqn_for(repo / relative, package_root)
        _collect(tree.body, module, surface)
    return {fqn: signature for fqn, signature in surface.items() if _is_public(fqn)}


def _collect(body: list[ast.stmt], prefix: str, out: dict[str, str]) -> None:
    for node in body:
        if isinstance(node, ast.ClassDef):
            fqn = f"{prefix}.{node.name}"
            out[fqn] = "class"
            _collect(node.body, fqn, out)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            fqn = f"{prefix}.{node.name}"
            out[fqn] = _signature(node)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    arguments = node.args
    names = [argument.arg for argument in [*arguments.posonlyargs, *arguments.args]]
    if arguments.vararg:
        names.append(f"*{arguments.vararg.arg}")
    elif arguments.kwonlyargs:
        names.append("*")
    names.extend(argument.arg for argument in arguments.kwonlyargs)
    if arguments.kwarg:
        names.append(f"**{arguments.kwarg.arg}")
    # Defaults change what callers may omit, so their count is part of the signature.
    optional = len(arguments.defaults) + sum(1 for d in arguments.kw_defaults if d is not None)
    return f"({', '.join(names)}) defaults={optional}"


def _is_public(fqn: str) -> bool:
    return not any(part.startswith("_") for part in fqn.split("."))
