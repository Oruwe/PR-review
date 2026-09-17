"""The verification spine, end to end.

Blast radius -> generate -> validate on base -> run survivors on head. A test that
passed on base and fails on head is a behaviour change, whether or not the author knew.

What the PR *claims* to change is extracted separately. A finding inside that claim is
not wrong, it is declared; a finding outside it is the product: "this behaviour changed
and the PR did not say it would."
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import structlog

from prflagger.analysis.blast import blast_radius, changed_symbols
from prflagger.characterize.generate import generate_tests
from prflagger.characterize.validate import (
    _record,
    base_checkout,
    run_tests_on,
    validate_on_base,
)
from prflagger.gitsafety import assert_safe_revision
from prflagger.llm import complete
from prflagger.models import Finding, Symbol
from prflagger.providers import DEFAULT_MODEL

__all__ = ["BEHAVIOR_CHANGE_SEVERITY", "declared_scope", "differential"]

log = structlog.get_logger(__name__)

BEHAVIOR_CHANGE_SEVERITY = 1.0

# A test that survived base validation twice and then failed on head is strong evidence,
# but it is still a generated test.
_BASE_CONFIDENCE = 0.9

# Characterizing every symbol in a large diff is neither affordable nor necessary. What
# is skipped is counted and reported — never silently dropped.
_MAX_SYMBOLS = int(os.environ.get("PRFLAGGER_MAX_SYMBOLS", "5"))

_SCOPE_PROMPT = """\
Here is a pull request's title and description.

Title: {title}

{body}

List the code symbols whose behaviour this pull request says it changes. Use the names
as written in the description — function, method or class names, one per line. Do not
infer symbols that are merely implied, and do not invent any. If the description names
none, return nothing.

Return only the names, one per line, no prose, no bullets, no markdown.
"""

_FAILURE_HEADER = re.compile(r"^_+ (\S+) _+$")


def differential(repo: Path, base: str, head: str) -> list[Finding]:
    """Full pipeline: blast radius -> generate -> validate on base -> run survivors on
    head -> emit a Finding per test that passed on base and failed on head."""
    started = time.monotonic()
    radius = _touched_first(repo, base, head)
    selected = radius[:_MAX_SYMBOLS]
    skipped = len(radius) - len(selected)

    base_tree = base_checkout(repo, base)
    head_tree = base_checkout(repo, head)
    declared = {name.lower() for name in declared_scope(*_pr_text(repo, head))}

    findings: list[Finding] = []
    for symbol in selected:
        source = _source_of(base_tree, symbol)
        if source is None:
            continue
        survivors, discard_rate = validate_on_base(
            generate_tests(symbol, source), repo=base_tree, base_sha=base
        )
        if not survivors:
            log.info("no_surviving_tests", symbol=symbol.fqn, discard_rate=discard_rate)
            continue

        outcomes, output = run_tests_on(survivors, head_tree, head, label="head")
        blocks = _failure_blocks(output)
        for index, outcome in sorted(outcomes.items()):
            if outcome == "passed":
                continue
            findings.append(_finding(symbol, index, blocks, declared))

    _record(
        {
            "stage": "differential",
            "base": base,
            "head": head,
            "symbols_in_radius": len(radius),
            "symbols_attempted": len(selected),
            "symbols_skipped": skipped,
            "findings": len(findings),
            "declared_symbols": sorted(declared),
            "wall_s": round(time.monotonic() - started, 3),
        }
    )
    log.info("differential", findings=len(findings), attempted=len(selected), skipped=skipped)
    return findings


def _touched_first(repo: Path, base: str, head: str) -> list[Symbol]:
    """Blast radius, ordered so the symbols the diff actually changed come first.

    Callers matter, but behaviour flips where the edit landed. Under a cap, ordering by
    name would drop the changed symbol for an alphabetical accident.
    """
    radius = blast_radius(repo, base, head)
    touched = {symbol.fqn for symbol in changed_symbols(repo, base, head)}
    return sorted(radius, key=lambda symbol: (symbol.fqn not in touched, symbol.fqn))


def _finding(
    symbol: Symbol, index: int, blocks: dict[str, str], declared: set[str]
) -> Finding:
    nodeid = f"test_char_{index}"
    evidence = blocks.get(nodeid, "").strip()
    is_declared = _is_declared(symbol, declared)
    return Finding(
        kind="behavior_change",
        symbol=symbol.fqn,
        what_changed=(
            f"{symbol.fqn}: behaviour recorded on base no longer holds at head"
            + (" (declared in the PR description)" if is_declared else "")
        ),
        # The nodeid plus the assertion diff: what was asserted and what happened.
        how_we_know=f"{nodeid}\n{evidence}" if evidence else nodeid,
        norm=None,  # C10 attaches one; a behaviour change is self-justifying without it
        confidence=_BASE_CONFIDENCE * (0.5 if is_declared else 1.0),
        severity=BEHAVIOR_CHANGE_SEVERITY,
    )


def _is_declared(symbol: Symbol, declared: set[str]) -> bool:
    """A claim naming `split_arg_string` covers `click.shell_completion.split_arg_string`."""
    fqn = symbol.fqn.lower()
    short = fqn.rsplit(".", 1)[-1]
    return any(
        claim in (fqn, short) or fqn.endswith(f".{claim}") for claim in declared
    )


def declared_scope(pr_title: str, pr_body: str) -> list[str]:
    """LLM extraction of the symbols/behaviors the PR claims to change."""
    if not pr_title.strip() and not pr_body.strip():
        return []
    response = complete(
        _SCOPE_PROMPT.format(title=pr_title.strip(), body=pr_body.strip()),
        model=DEFAULT_MODEL,
        max_tokens=1024,
    )
    names: list[str] = []
    for line in response.splitlines():
        candidate = line.strip().strip("`-*• ").split("(")[0].strip()
        if candidate and " " not in candidate:
            names.append(candidate)
    return names


def _pr_text(repo: Path, head: str) -> tuple[str, str]:
    """The head commit's subject and body.

    For a single-commit branch this is what the PR description says — the claim the
    system measures the diff against.
    """
    assert_safe_revision(head)
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "log", "-1", "--format=%s%n%x00%n%b", head],
        capture_output=True,
        text=True,
        check=False,
    )
    subject, _, body = completed.stdout.partition("\x00")
    return subject.strip(), body.strip()


def _source_of(checkout: Path, symbol: Symbol) -> str | None:
    path = checkout / symbol.file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    segment = "\n".join(lines[symbol.line_start - 1 : symbol.line_end])
    return f"# {symbol.file}\n# {symbol.fqn}\n\n{segment}" if segment.strip() else None


def _failure_blocks(output: str) -> dict[str, str]:
    """pytest's FAILURES section, split per test, so each finding cites its own diff."""
    blocks: dict[str, str] = {}
    current: str | None = None
    collected: list[str] = []
    for line in output.splitlines():
        match = _FAILURE_HEADER.match(line.strip())
        if match is not None:
            if current is not None:
                blocks[current] = "\n".join(collected).strip()
            current = match.group(1)
            collected = []
            continue
        if current is not None:
            if line.startswith("=") and "short test summary" in line:
                break
            collected.append(line)
    if current is not None:
        blocks[current] = "\n".join(collected).strip()
    return blocks
