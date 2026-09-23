"""The Python pack: pytest, ruff, mypy, coverage.

This is the same toolchain v1 hardcoded, expressed as data. The parsers are
lifted from v1's `probes/lint.py` and `probes/coverage.py` unchanged in
behaviour — they were correct, they were just unreachable from any other
ecosystem.
"""

from __future__ import annotations

import json

from prflagger.lang.base import (
    CoverageCommand,
    LintCommand,
    TestCommand,
    Toolchain,
    embedded_json,
    register_coverage_parser,
    register_lint_parser,
    register_test_parser,
    split_trailing_code,
)

__all__ = ["PYTHON"]


@register_test_parser("pytest-json")
def _parse_pytest(stdout: str) -> dict[str, str]:
    report = embedded_json(stdout, must_have="tests")
    if report is None:
        return {}
    tests = report.get("tests")
    if not isinstance(tests, list):
        return {}
    return {
        str(entry["nodeid"]): str(entry.get("outcome", "error"))
        for entry in tests
        if isinstance(entry, dict) and "nodeid" in entry
    }


@register_lint_parser("ruff-json")
def _parse_ruff(text: str) -> list[tuple[str, str, str]]:
    try:
        entries = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    out: list[tuple[str, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename", ""))
        code = str(entry.get("code") or "")
        message = str(entry.get("message", ""))
        out.append((filename, code, message))
    return out


@register_lint_parser("mypy-text")
def _parse_mypy(text: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if ": error:" not in line and ": note:" not in line:
            continue
        path, _, remainder = line.partition(":")
        if ": error:" not in line:
            continue
        _, _, message = remainder.partition("error:")
        body, code = split_trailing_code(message)
        out.append((path.strip(), code or "error", body))
    return out


@register_coverage_parser("coverage-json")
def _parse_coverage(stdout: str) -> dict[str, set[int]]:
    report = embedded_json(stdout, must_have="files")
    if report is None:
        return {}
    files = report.get("files")
    if not isinstance(files, dict):
        return {}
    executed: dict[str, set[int]] = {}
    for path, entry in files.items():
        if not isinstance(entry, dict):
            continue
        lines = entry.get("executed_lines") or []
        if isinstance(lines, list):
            executed[str(path)] = {int(n) for n in lines if isinstance(n, int)}
    return executed


#: pytest-json-report writes to a path; the container root is read-only, so it
#: goes to /dev/stdout and the parser scans it back out.
_PYTEST = (
    "pytest", "-p", "no:cacheprovider", "-q",
    "--json-report", "--json-report-file=/dev/stdout",
)

PYTHON = Toolchain(
    id="python",
    display="Python",
    markers=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"),
    base_image="python:3.11-slim",
    setup_lines=(
        "RUN pip install --no-cache-dir --disable-pip-version-check"
        " pytest pytest-json-report pytest-cov coverage ruff mypy",
    ),
    install=(("pip", "install", "--no-cache-dir", "--disable-pip-version-check", "/build"),),
    test=TestCommand(argv=_PYTEST, report_format="pytest-json", selector_flag="-k"),
    lints=(
        LintCommand("ruff", ("ruff", "check", "--output-format=json", "."), "ruff-json"),
        LintCommand(
            "mypy", ("mypy", "--no-error-summary", "."), "mypy-text", ok_codes=(0, 1, 2)
        ),
    ),
    coverage=CoverageCommand(
        argv=(
            "sh", "-c",
            "coverage run -m pytest -p no:cacheprovider -q >/dev/null 2>&1; "
            "coverage json -o /dev/stdout 2>/dev/null",
        ),
        parser="coverage-json",
    ),
    grammar="python",
    source_globs=("**/*.py",),
    test_globs=("**/test_*.py", "**/*_test.py", "tests/**/*.py"),
    lockfiles=(
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
        "requirements-dev.txt", "uv.lock", "poetry.lock", "Pipfile.lock",
    ),
    env=(("PYTHONDONTWRITEBYTECODE", "1"), ("PYTHONUNBUFFERED", "1")),
)
