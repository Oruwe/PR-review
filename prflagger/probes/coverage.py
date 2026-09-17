"""Coverage over the lines the diff actually changed.

Repo-wide coverage is a vanity number. What matters is whether the code this PR added
is exercised at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from prflagger.analysis.blast import changed_symbols, package_root_for
from prflagger.characterize.validate import base_checkout
from prflagger.models import Finding, Job, Symbol
from prflagger.probes._support import SEVERITY, norm_for
from prflagger.sandbox.calibrate import calibrate
from prflagger.sandbox.runner import lockfile_image_key, run_job

__all__ = ["coverage_delta", "executed_lines"]

log = structlog.get_logger(__name__)


def coverage_delta(repo: Path, base: str, head: str) -> list[Finding]:
    """coverage.py over changed lines. Finding per changed symbol with 0 covered lines."""
    norm = norm_for("coverage_gap", repo)
    if norm is None:
        log.info("coverage.no_declared_norm", repo=str(repo))
        return []

    touched = changed_symbols(repo, base, head)
    if not touched:
        return []

    checkout = base_checkout(repo, head)
    covered = executed_lines(checkout, head, package_root_for(repo).name)
    if covered is None:
        # Could not measure — say so rather than reporting everything as uncovered.
        log.warning("coverage.unavailable", repo=str(repo), head=head[:8])
        return []

    findings: list[Finding] = []
    for symbol in touched:
        lines = _lines_for(covered, symbol)
        if lines is None:
            continue
        # The `def` line runs at import, so it is covered even when the body never
        # executes. Only the body is evidence that something called this.
        body_start = symbol.line_start + 1
        hit = sum(1 for line in lines if body_start <= line <= symbol.line_end)
        total = max(0, symbol.line_end - symbol.line_start)
        if total == 0:
            continue
        if hit == 0:
            findings.append(
                Finding(
                    kind="coverage_gap",
                    symbol=symbol.fqn,
                    what_changed=(
                        f"{symbol.fqn} was changed and no test executes it: "
                        f"{total} lines added or changed, 0 covered"
                    ),
                    how_we_know=(
                        f"coverage.py over {symbol.file}:"
                        f"{body_start}-{symbol.line_end} after running the "
                        f"repository's own suite: 0 of {total} body lines executed"
                    ),
                    norm=norm,
                    confidence=1.0,
                    severity=SEVERITY["coverage_gap"],
                )
            )
    log.info("coverage_delta", changed=len(touched), findings=len(findings))
    return findings


def _lines_for(covered: dict[str, set[int]], symbol: Symbol) -> set[int] | None:
    """Coverage keys paths as the run saw them; match on suffix."""
    for path, lines in covered.items():
        if path.endswith(symbol.file):
            return lines
    return None


def executed_lines(checkout: Path, commit: str, package: str) -> dict[str, set[int]] | None:
    """Run the repo's own suite under coverage. None when the measurement failed."""
    tests = "tests" if (checkout / "tests").is_dir() else "."
    memory_mb, timeout_s = calibrate(checkout, commit)
    result = run_job(
        Job(
            repo_path=str(checkout.resolve()),
            commit=commit,
            image_key=lockfile_image_key(checkout),
            command=(
                "sh",
                "-c",
                # The root filesystem is read-only, so coverage's data file lives in the
                # tmpfs and the report goes to stdout rather than a file that would
                # vanish with the container.
                "COVERAGE_FILE=/tmp/.coverage coverage run "
                f"--source={package} -m pytest -q -p no:cacheprovider {tests} "
                "> /tmp/pytest.log 2>&1; "
                "COVERAGE_FILE=/tmp/.coverage coverage json -o - 2>/dev/null",
            ),
            # Coverage instrumentation makes the suite slower and hungrier.
            timeout_s=max(timeout_s * 4, 120),
            memory_mb=max(memory_mb * 2, 512),
        )
    )
    return _parse(result.stdout)


def _parse(stdout: str) -> dict[str, set[int]] | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout, index)
        except ValueError:
            continue
        if isinstance(payload, dict) and "files" in payload:
            return _executed(payload["files"])
    return None


def _executed(files: dict[str, Any]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for path, entry in files.items():
        if isinstance(entry, dict):
            out[path] = set(entry.get("executed_lines", []))
    return out
