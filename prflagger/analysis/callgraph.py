"""A name-resolved call graph, built from `ast`.

False positives are acceptable; false negatives are not. A spurious edge costs a few
wasted test generations. A missed caller means a behaviour change ships unnoticed,
which is the one failure this system exists to prevent.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from prflagger.analysis.symbols import module_fqn_for, symbols_in_file
from prflagger.models import Symbol

__all__ = ["build_call_graph", "index_symbols", "package_files"]


#: Directories that are never the project's own source. Walking into them from a
#: repository root means parsing a virtualenv or a vendored tree on every build.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".nox", "build", "dist", "target", "vendor", "site-packages",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs",
})


def package_files(package_root: Path) -> list[Path]:
    """Every Python file under `package_root`, skipping what is not its source."""
    found: list[Path] = []
    stack = [package_root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix == ".py" and entry.is_file():
                found.append(entry)
    return sorted(found)


def index_symbols(package_root: Path) -> dict[str, Symbol]:
    """fqn -> Symbol for every definition in the package."""
    index: dict[str, Symbol] = {}
    for path in package_files(package_root):
        for symbol in symbols_in_file(path, module_fqn_for(path, package_root)):
            index[symbol.fqn] = symbol
    return index


#: A bare-name call matching more than this many symbols is not resolution, it is
#: a coincidence of naming. Recording all of them does not add a false positive —
#: it adds dozens, and on a repository where `run` or `get` is defined in forty
#: places it draws an edge from every module to every other, which makes the
#: dependency graph say nothing. SPEC.md accepts false positives over false
#: negatives; that holds for a plausible handful, not for an exhaustive list.
_MAX_NAME_MATCHES = 8


def build_call_graph(package_root: Path) -> dict[str, set[str]]:
    """caller fqn -> set of callee fqns.

    Name-based resolution: resolve imports where possible, fall back to matching on the
    bare attribute/function name, and drop a fallback match too ambiguous to mean
    anything (see `_MAX_NAME_MATCHES`).
    """
    symbols = index_symbols(package_root)
    by_name: dict[str, set[str]] = defaultdict(set)
    for fqn in symbols:
        by_name[fqn.rsplit(".", 1)[-1]].add(fqn)

    graph: dict[str, set[str]] = {}
    for path in package_files(package_root):
        module = module_fqn_for(path, package_root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        collector = _CallCollector(module, _imports(tree, module), symbols, by_name)
        collector.visit_body(tree.body, enclosing=None, prefix=module, inside_class=False)
        for caller, callees in collector.edges.items():
            graph.setdefault(caller, set()).update(callees)
    return graph


def _imports(tree: ast.Module, module: str) -> dict[str, str]:
    """Local alias -> dotted target, for both `import x.y as z` and `from x import y`."""
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mapping[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import: resolve against this module's package
                package = module.rsplit(".", node.level)[0] if "." in module else module
                base = f"{package}.{base}" if base else package
            for alias in node.names:
                target = f"{base}.{alias.name}" if base else alias.name
                mapping[alias.asname or alias.name] = target
    return mapping


class _CallCollector:
    def __init__(
        self,
        module: str,
        imports: dict[str, str],
        symbols: dict[str, Symbol],
        by_name: dict[str, set[str]],
    ) -> None:
        self.module = module
        self.imports = imports
        self.symbols = symbols
        self.by_name = by_name
        self.edges: dict[str, set[str]] = defaultdict(set)

    def _fallback(self, name: str) -> set[str]:
        """The bare-name match, unless the name is too common to carry meaning."""
        matches = self.by_name.get(name, ())
        if len(matches) > _MAX_NAME_MATCHES:
            return set()
        return set(matches)

    def visit_body(
        self, body: list[ast.stmt], *, enclosing: str | None, prefix: str, inside_class: bool
    ) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                # A decorator or base-class expression is not a call from inside the class.
                self.visit_body(
                    node.body,
                    enclosing=enclosing,
                    prefix=f"{prefix}.{node.name}",
                    inside_class=True,
                )
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                fqn = f"{prefix}.{node.name}"
                # Calls belong to the innermost enclosing function, which is a symbol
                # in its own right.
                self.visit_body(
                    node.body, enclosing=fqn, prefix=fqn, inside_class=False
                )
            else:
                if enclosing is not None:
                    self._record_calls(node, enclosing)
                for inner in _statement_bodies(node):
                    self.visit_body(
                        inner, enclosing=enclosing, prefix=prefix, inside_class=inside_class
                    )

    def _record_calls(self, node: ast.stmt, caller: str) -> None:
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            for callee in self._resolve(descendant.func):
                if callee != caller:
                    self.edges[caller].add(callee)

    def _resolve(self, func: ast.expr) -> set[str]:
        if isinstance(func, ast.Name):
            local = f"{self.module}.{func.id}"
            if local in self.symbols:
                return {local}
            imported = self.imports.get(func.id)
            if imported and imported in self.symbols:
                return {imported}
            return self._fallback(func.id)
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                base = self.imports.get(func.value.id)
                if base:
                    qualified = f"{base}.{func.attr}"
                    if qualified in self.symbols:
                        return {qualified}
            # `obj.method()` — the receiver's type is unknown, so match the bare name.
            return self._fallback(func.attr)
        return set()


def _statement_bodies(node: ast.stmt) -> list[list[ast.stmt]]:
    bodies: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(node, field, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            bodies.append(value)
    for handler in getattr(node, "handlers", []) or []:
        if isinstance(handler, ast.ExceptHandler):
            bodies.append(handler.body)
    return bodies
