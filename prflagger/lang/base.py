"""The toolchain abstraction: what "run this repo's tests" means, per ecosystem.

v1 hardcoded `pytest`, `ruff`, `mypy` and `coverage` at a dozen call sites, which
is why it only ever worked on one Python repo. A pack answers four questions for
any repo — what image, how to install, how to test, how to lint — and everything
above this module asks the pack instead of assuming.

Parsers are named rather than stored as callables so a `Toolchain` stays a frozen,
JSON-serialisable dataclass. The Atlas view shows the exact commands a repo will
be run with, and it can only do that if the pack survives a round trip through
the database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "CoverageCommand",
    "LintCommand",
    "TestCommand",
    "Toolchain",
    "derived_install",
    "parse_coverage",
    "parse_lint",
    "parse_tests",
    "register_coverage_parser",
    "register_install_deriver",
    "register_lint_parser",
    "register_test_parser",
]


@dataclass(frozen=True)
class TestCommand:
    """How to run the suite and read back per-test results.

    `argv` must produce machine-readable output on stdout. The container root is
    read-only, so nothing may be written to a file that vanishes with it.
    """

    argv: tuple[str, ...]
    report_format: str = "none"  # key into the test-parser registry
    selector_flag: str = ""  # how to run one test, e.g. "-k" or "-run"


@dataclass(frozen=True)
class LintCommand:
    tool: str
    argv: tuple[str, ...]
    parser: str
    #: Exit codes that mean "ran fine, found nothing". Most linters use 1 for
    #: "found problems", which is a result, not a failure to run.
    ok_codes: tuple[int, ...] = (0, 1)


@dataclass(frozen=True)
class CoverageCommand:
    argv: tuple[str, ...]
    parser: str


@dataclass(frozen=True)
class Toolchain:
    """One ecosystem's answer to how a repo is built, tested and checked."""

    id: str
    display: str
    markers: tuple[str, ...]  # files whose presence identifies this ecosystem
    base_image: str
    install: tuple[tuple[str, ...], ...] = ()
    test: TestCommand = field(default_factory=lambda: TestCommand(argv=()))
    lints: tuple[LintCommand, ...] = ()
    coverage: CoverageCommand | None = None
    grammar: str = ""  # tree-sitter language name, "" when unsupported
    source_globs: tuple[str, ...] = ()
    test_globs: tuple[str, ...] = ()
    lockfiles: tuple[str, ...] = ()  # files whose contents key the image
    env: tuple[tuple[str, str], ...] = ()
    #: Extra Dockerfile lines, inserted after the base image. Packs that need a
    #: toolchain installed (linters, reporters) put it here.
    setup_lines: tuple[str, ...] = ()
    #: Install commands read from the repository's own files, run after `install`.
    #: Named, like the parsers, so the pack stays serialisable; see `derived_install`.
    install_from_repo: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe view. This is what the Atlas view renders."""
        return {
            "id": self.id,
            "display": self.display,
            "base_image": self.base_image,
            "install": [list(c) for c in self.install],
            "install_from_repo": list(self.install_from_repo),
            "test": {"argv": list(self.test.argv), "report_format": self.test.report_format},
            "lints": [{"tool": lint.tool, "argv": list(lint.argv)} for lint in self.lints],
            "coverage": list(self.coverage.argv) if self.coverage else None,
            "grammar": self.grammar,
            "source_globs": list(self.source_globs),
            "test_globs": list(self.test_globs),
        }


# ----------------------------------------------------------------------------------
# Parser registries
# ----------------------------------------------------------------------------------

TestParser = Callable[[str], dict[str, str]]
LintParser = Callable[[str], list[tuple[str, str, str]]]
CoverageParser = Callable[[str], dict[str, set[int]]]

_TEST_PARSERS: dict[str, TestParser] = {}
_LINT_PARSERS: dict[str, LintParser] = {}
_COVERAGE_PARSERS: dict[str, CoverageParser] = {}


def register_test_parser(name: str) -> Callable[[TestParser], TestParser]:
    def decorate(fn: TestParser) -> TestParser:
        _TEST_PARSERS[name] = fn
        return fn

    return decorate


def register_lint_parser(name: str) -> Callable[[LintParser], LintParser]:
    def decorate(fn: LintParser) -> LintParser:
        _LINT_PARSERS[name] = fn
        return fn

    return decorate


def register_coverage_parser(name: str) -> Callable[[CoverageParser], CoverageParser]:
    def decorate(fn: CoverageParser) -> CoverageParser:
        _COVERAGE_PARSERS[name] = fn
        return fn

    return decorate


InstallDeriver = Callable[[Path], tuple[tuple[str, ...], ...]]
_INSTALL_DERIVERS: dict[str, InstallDeriver] = {}


def register_install_deriver(name: str) -> Callable[[InstallDeriver], InstallDeriver]:
    def decorate(fn: InstallDeriver) -> InstallDeriver:
        _INSTALL_DERIVERS[name] = fn
        return fn

    return decorate


def derived_install(toolchain: Toolchain, repo_path: Path) -> tuple[tuple[str, ...], ...]:
    """The install commands `toolchain` reads from the repository at `repo_path`.

    A deriver never raises: a file it cannot read means nothing to install.
    """
    commands: list[tuple[str, ...]] = []
    for name in toolchain.install_from_repo:
        deriver = _INSTALL_DERIVERS.get(name)
        if deriver is not None:
            commands.extend(deriver(repo_path))
    return tuple(commands)


def parse_tests(report_format: str, stdout: str) -> dict[str, str]:
    """nodeid -> "passed"|"failed"|"error"|"skipped". Unknown format yields nothing."""
    parser = _TEST_PARSERS.get(report_format)
    return parser(stdout) if parser else {}


def parse_lint(parser_name: str, output: str) -> list[tuple[str, str, str]]:
    """(path, code, message) per diagnostic."""
    parser = _LINT_PARSERS.get(parser_name)
    return parser(output) if parser else []


def parse_coverage(parser_name: str, stdout: str) -> dict[str, set[int]]:
    """file path -> executed line numbers."""
    parser = _COVERAGE_PARSERS.get(parser_name)
    return parser(stdout) if parser else {}


# ----------------------------------------------------------------------------------
# Shared helpers the packs reuse
# ----------------------------------------------------------------------------------


def embedded_json(stdout: str, *, must_have: str) -> dict[str, Any] | None:
    """Pull one JSON object out of noisy stdout.

    Reporters print their document alongside human output, and the container is
    read-only so it cannot be written to a file instead. Scanning for the first
    object carrying `must_have` is how the document is recovered.
    """
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(stdout, index)
        except ValueError:
            continue
        if isinstance(candidate, dict) and must_have in candidate:
            return candidate
    return None


def json_lines(stdout: str) -> list[dict[str, Any]]:
    """Every standalone JSON object in a stream of them, ignoring other output."""
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


_TRAILING_CODE = re.compile(r"\s*\[([a-z][a-z0-9-]*)\]\s*$")


def split_trailing_code(message: str) -> tuple[str, str]:
    """Split `"msg  [code]"` into `(msg, code)`; `("msg", "")` when there is none."""
    match = _TRAILING_CODE.search(message)
    if not match:
        return message.strip(), ""
    return message[: match.start()].strip(), match.group(1)
