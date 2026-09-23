"""The Go pack: `go test -json`, `go vet`.

`go test -json` emits one JSON object per event rather than a single document,
so the parser folds the event stream down to a final status per test.
"""

from __future__ import annotations

from prflagger.lang.base import (
    LintCommand,
    TestCommand,
    Toolchain,
    json_lines,
    register_lint_parser,
    register_test_parser,
)

__all__ = ["GO"]

_ACTION = {"pass": "passed", "fail": "failed", "skip": "skipped"}


@register_test_parser("go-json")
def _parse_go(stdout: str) -> dict[str, str]:
    results: dict[str, str] = {}
    for event in json_lines(stdout):
        name = event.get("Test")
        action = _ACTION.get(str(event.get("Action", "")))
        if not name or action is None:
            continue  # package-level and output events carry no per-test verdict
        results[f"{event.get('Package', '')}::{name}"] = action
    return results


@register_lint_parser("govet-text")
def _parse_vet(text: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split(":", 3)
        if len(parts) < 4 or not parts[1].isdigit():
            continue
        out.append((parts[0].strip(), "vet", parts[3].strip()))
    return out


GO = Toolchain(
    id="go",
    display="Go",
    markers=("go.mod",),
    base_image="golang:1.23-bookworm",
    install=(("go", "mod", "download"),),
    test=TestCommand(argv=("go", "test", "-json", "./..."), report_format="go-json",
                     selector_flag="-run"),
    lints=(LintCommand("vet", ("go", "vet", "./..."), "govet-text", ok_codes=(0, 1, 2)),),
    grammar="go",
    source_globs=("**/*.go",),
    test_globs=("**/*_test.go",),
    lockfiles=("go.mod", "go.sum"),
    env=(("CGO_ENABLED", "0"), ("GOFLAGS", "-mod=mod")),
)
