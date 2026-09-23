"""The Node pack: vitest/jest, eslint, tsc.

Both major test runners emit a JSON document with a `testResults` array, so one
parser serves them. Which runner a repo uses is decided at detection time from
its `package.json`, not guessed at run time.
"""

from __future__ import annotations

import json

from prflagger.lang.base import (
    LintCommand,
    TestCommand,
    Toolchain,
    embedded_json,
    register_lint_parser,
    register_test_parser,
)

__all__ = ["NODE"]

#: jest and vitest both report `status` per assertion; normalise to our vocabulary.
_STATUS = {
    "passed": "passed",
    "failed": "failed",
    "pending": "skipped",
    "skipped": "skipped",
    "todo": "skipped",
}


@register_test_parser("jest-json")
def _parse_jest(stdout: str) -> dict[str, str]:
    report = embedded_json(stdout, must_have="testResults")
    if report is None:
        return {}
    results: dict[str, str] = {}
    for suite in report.get("testResults") or []:
        if not isinstance(suite, dict):
            continue
        suite_name = str(suite.get("name") or suite.get("testFilePath") or "")
        for case in suite.get("assertionResults") or []:
            if not isinstance(case, dict):
                continue
            title = " > ".join(
                [*(case.get("ancestorTitles") or []), str(case.get("title", ""))]
            )
            nodeid = f"{suite_name}::{title}" if suite_name else title
            results[nodeid] = _STATUS.get(str(case.get("status", "")), "error")
    return results


@register_lint_parser("eslint-json")
def _parse_eslint(text: str) -> list[tuple[str, str, str]]:
    try:
        entries = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, str, str]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("filePath", ""))
        for message in entry.get("messages") or []:
            if not isinstance(message, dict):
                continue
            out.append(
                (path, str(message.get("ruleId") or "eslint"), str(message.get("message", "")))
            )
    return out


@register_lint_parser("tsc-text")
def _parse_tsc(text: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if "error TS" not in line:
            continue
        path, _, remainder = line.partition("(")
        code, _, message = remainder.partition("error ")
        out.append((path.strip(), message.split(":", 1)[0].strip() or "TS", message.strip()))
    return out


NODE = Toolchain(
    id="node",
    display="JavaScript / TypeScript",
    markers=("package.json",),
    base_image="node:22-slim",
    install=(("npm", "ci", "--no-audit", "--no-fund"),),
    test=TestCommand(
        argv=("npx", "--no-install", "vitest", "run", "--reporter=json"),
        report_format="jest-json",
        selector_flag="-t",
    ),
    lints=(
        LintCommand(
            "eslint", ("npx", "--no-install", "eslint", "-f", "json", "."), "eslint-json"
        ),
        LintCommand(
            "tsc", ("npx", "--no-install", "tsc", "--noEmit"), "tsc-text", ok_codes=(0, 1, 2)
        ),
    ),
    grammar="typescript",
    source_globs=("**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.mjs"),
    test_globs=("**/*.test.*", "**/*.spec.*", "test/**/*", "tests/**/*", "__tests__/**/*"),
    lockfiles=("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
    env=(("CI", "1"), ("NODE_ENV", "test")),
)
