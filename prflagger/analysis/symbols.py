"""Symbols from `ast`. Exact, deterministic, and free."""

from __future__ import annotations

import ast
from pathlib import Path

from prflagger.models import Symbol

__all__ = ["module_fqn_for", "symbols_in_file"]


def symbols_in_file(path: Path, module_fqn: str) -> list[Symbol]:
    """Parse with ast. Nested functions get dotted fqns. Decorators do not create
    symbols."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    found: list[Symbol] = []
    _walk(tree.body, prefix=module_fqn, file=path.as_posix(), inside_class=False, out=found)
    return found


def _walk(
    body: list[ast.stmt],
    *,
    prefix: str,
    file: str,
    inside_class: bool,
    out: list[Symbol],
) -> None:
    for node in body:
        if isinstance(node, ast.ClassDef):
            fqn = f"{prefix}.{node.name}"
            out.append(_symbol(node, fqn, "class", file))
            _walk(node.body, prefix=fqn, file=file, inside_class=True, out=out)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            fqn = f"{prefix}.{node.name}"
            out.append(_symbol(node, fqn, "method" if inside_class else "function", file))
            # Nested functions get dotted fqns; a function body is never a class body.
            _walk(node.body, prefix=fqn, file=file, inside_class=False, out=out)
        else:
            # Definitions inside if/try/with blocks are still definitions.
            for inner in _nested_bodies(node):
                _walk(inner, prefix=prefix, file=file, inside_class=inside_class, out=out)


def _nested_bodies(node: ast.stmt) -> list[list[ast.stmt]]:
    bodies: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(node, field, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            bodies.append(value)
    for handler in getattr(node, "handlers", []) or []:
        if isinstance(handler, ast.ExceptHandler):
            bodies.append(handler.body)
    return bodies


def _symbol(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, fqn: str, kind: str, file: str
) -> Symbol:
    # `node.lineno` is the `def`/`class` line, not the first decorator: a decorator is
    # not a symbol and must not widen the one it decorates.
    return Symbol(
        fqn=fqn,
        kind=kind,
        file=file,
        line_start=node.lineno,
        line_end=node.end_lineno or node.lineno,
    )


def module_fqn_for(path: Path, package_root: Path) -> str:
    """`src/click/parser.py` under `src/click` -> `click.parser`."""
    relative = path.relative_to(package_root)
    parts = [package_root.name, *relative.parts]
    if parts[-1] == "__init__.py":
        parts.pop()
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(parts)
