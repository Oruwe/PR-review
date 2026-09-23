"""Poetry's development dependencies, as requirements pip can install.

Poetry declares a suite's own dependencies in `[tool.poetry.group.<name>.dependencies]`
(or, before Poetry 1.2, `[tool.poetry.dev-dependencies]`), in its own constraint
syntax. pip reads neither, so a repository that keeps `pytest` or `attrs` there
stopped at collection in the sandbox — Textualize/rich among them.

This is a translation, not a resolver: each constraint becomes the PEP 508
requirement Poetry documents it to mean, and pip resolves them. Nothing here reads
a lock file or the network. A dependency that is not a version (`git`, `path`,
`url`) is left out rather than guessed at.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = ["dev_requirements", "requirement", "specifier"]

#: The group whose dependencies a suite needs: the first test-shaped one a
#: repository declares, else its development group. The same order the pack uses
#: for PEP 735 groups.
_TEST_GROUPS = ("test", "tests", "testing")
_DEV_GROUP = "dev"

_RELEASE = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")
_OPERATOR = re.compile(r"^(===|~=|==|!=|<=|>=|<|>|=)\s*(.+)$")
_NOT_A_VERSION = ("git", "path", "url", "file")


def dev_requirements(pyproject: Mapping[str, Any]) -> tuple[str, ...]:
    """The requirements of the group a test suite needs, or () if Poetry declares none."""
    poetry = _table(_table(pyproject, "tool"), "poetry")
    groups = _table(poetry, "group")
    for name in _TEST_GROUPS:
        dependencies = _table(_table(groups, name), "dependencies")
        if dependencies:
            return _requirements(dependencies)
    development = {
        **_table(poetry, "dev-dependencies"),
        **_table(_table(groups, _DEV_GROUP), "dependencies"),
    }
    return _requirements(development)


def requirement(name: str, declared: Any) -> list[str]:
    """PEP 508 requirements for one Poetry dependency; several for a multi-constraint list."""
    if name.lower() == "python":
        return []
    if isinstance(declared, list):
        return [r for entry in declared for r in requirement(name, entry)]
    if isinstance(declared, str):
        return [f"{name}{specifier(declared)}"]
    if not isinstance(declared, Mapping) or any(key in declared for key in _NOT_A_VERSION):
        return []

    extras = declared.get("extras") or []
    head = f"{name}[{','.join(str(e) for e in extras)}]" if extras else name
    markers = []
    if declared.get("python"):
        markers.append(_python_marker(str(declared["python"])))
    if declared.get("markers"):
        markers.append(f"({declared['markers']})")
    tail = f" ; {' and '.join(markers)}" if markers else ""
    return [f"{head}{specifier(str(declared.get('version', '*')))}{tail}"]


def specifier(constraint: str) -> str:
    """A Poetry version constraint as a PEP 440 specifier ("" for any version).

    `^` and `~` follow Poetry's documented meaning: `^1.2.3` is `>=1.2.3,<2`,
    `^0.2.3` is `>=0.2.3,<0.3`, `~1.2` is `>=1.2,<1.3`. A bare version is exact.
    Poetry's `||` has no PEP 440 equivalent, so such a constraint is dropped and
    the latest release is installed instead.
    """
    text = constraint.strip()
    if not text or text == "*" or "||" in text:
        return ""
    parts = [_clause(clause.strip()) for clause in text.split(",") if clause.strip()]
    return ",".join(part for part in parts if part)


def _clause(clause: str) -> str:
    if clause.startswith("^"):
        return _bounded(clause[1:].strip(), caret=True)
    if clause.startswith("~") and not clause.startswith("~="):
        return _bounded(clause[1:].strip(), caret=False)
    operator = _OPERATOR.match(clause)
    if operator:
        symbol = "==" if operator.group(1) == "=" else operator.group(1)
        return f"{symbol}{operator.group(2).strip()}"
    if clause == "*":
        return ""
    return f"=={clause}"


def _bounded(version: str, *, caret: bool) -> str:
    match = _RELEASE.match(version)
    if match is None:
        return f"=={version}"
    release = [int(n) for n in match.group(1).split(".")]
    if caret:
        # Bump the first non-zero component; when every one given is zero, the last.
        index = next((i for i, n in enumerate(release) if n), len(release) - 1)
    else:
        index = 0 if len(release) == 1 else 1
    upper = [*release[:index], release[index] + 1]
    return f">={version},<{'.'.join(str(n) for n in upper)}"


def _python_marker(constraint: str) -> str:
    clauses = [c for c in specifier(constraint).split(",") if c]
    if not clauses:
        return "python_version >= '0'"
    marks = []
    for clause in clauses:
        found = _OPERATOR.match(clause)
        if found:
            marks.append(f"python_full_version {found.group(1)} '{found.group(2)}'")
    return "(" + " and ".join(marks) + ")"


def _requirements(dependencies: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(r for name, spec in dependencies.items() for r in requirement(str(name), spec))


def _table(value: Any, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    found = value.get(key)
    return found if isinstance(found, Mapping) else {}
