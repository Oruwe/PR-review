"""Norms a repository declares through its own configuration.

v1's `norms.declarative_norms` knew three Python tools. These come from the
toolchain pack instead — whatever linters the pack runs, for whatever language —
and each norm cites the exact file and line where the repository configures the
tool, so a citation of it can be checked like any other.

A declared norm is only created when the configuration really exists. The pack
running `ruff` on a repository that never configured it is the pack's choice,
not a standard the repository set itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from prflagger.core.models import Norm
from prflagger.lang.base import Toolchain

__all__ = ["declared_norms", "norm_for_tool"]

#: Where each tool's configuration may live: standalone files, or a section of a
#: shared file (matched by a line pattern, so the citation can name the line).
_CONFIG: dict[str, tuple[tuple[str, str | None], ...]] = {
    "ruff": (("ruff.toml", None), (".ruff.toml", None), ("pyproject.toml", r"^\[tool\.ruff")),
    "mypy": (("mypy.ini", None), (".mypy.ini", None), ("setup.cfg", r"^\[mypy"),
             ("pyproject.toml", r"^\[tool\.mypy")),
    "eslint": ((".eslintrc", None), (".eslintrc.js", None), (".eslintrc.cjs", None),
               (".eslintrc.json", None), (".eslintrc.yml", None), ("eslint.config.js", None),
               ("eslint.config.mjs", None), ("package.json", r'^\s*"eslintConfig"')),
    "tsc": (("tsconfig.json", None),),
    "vet": (("go.mod", r"^module "),),  # `go vet` is part of the toolchain the module declares
}

_TYPE_CHECKERS = frozenset({"mypy", "tsc"})
_CHANGELOGS = ("CHANGES.md", "CHANGELOG.md", "CHANGES.rst", "CHANGELOG.rst", "HISTORY.md",
               "HISTORY.rst", "NEWS.md", "CHANGES.txt")
_TEST_DIRS = ("tests", "test", "__tests__", "spec")


def norm_for_tool(tool: str) -> str:
    return f"declared-lint-{tool}"


def declared_norms(tree: Path, toolchain: Toolchain) -> list[Norm]:
    """What the repository at `tree` holds itself to, by its own configuration."""
    norms: list[Norm] = []
    for lint in toolchain.lints:
        where = _configured(tree, lint.tool)
        if where is None:
            continue
        checks = "type-check under" if lint.tool in _TYPE_CHECKERS else "pass"
        norms.append(_declared(
            norm_for_tool(lint.tool),
            f"Code must {checks} the repository's own {lint.tool} configuration.",
            where,
        ))

    changelog = next((name for name in _CHANGELOGS if (tree / name).is_file()), None)
    if changelog:
        norms.append(_declared(
            "declared-changelog",
            f"Changes to the public API are recorded in {changelog}.",
            f"{changelog}:1",
        ))

    tests = next((name for name in _TEST_DIRS if (tree / name).is_dir()), None)
    if tests:
        first = next((p for p in sorted((tree / tests).rglob("*")) if p.is_file()), None)
        if first is not None:
            norms.append(_declared(
                "declared-tests",
                "Changes keep the repository's own test suite passing, and new code is "
                "exercised by it.",
                f"{first.relative_to(tree).as_posix()}:1",
            ))
    return norms


def _configured(tree: Path, tool: str) -> str | None:
    """`path:line` of the tool's configuration in this repository, or None."""
    for name, pattern in _CONFIG.get(tool, ()):
        path = tree / name
        if not path.is_file():
            continue
        if pattern is None:
            return f"{name}:1"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            if re.search(pattern, line):
                return f"{name}:{index + 1}"
    return None


def _declared(norm_id: str, statement: str, where: str) -> Norm:
    return Norm(
        id=norm_id, statement=statement, scope="repo", support=0, distinct_reviewers=0,
        # Nothing was inferred: the repository automated or wrote down the rule itself.
        confidence=1.0, evidence_prs=(), source="declared", quote="",
        evidence=((0, "", where),), clustered_by="", named_by="config",
    )
