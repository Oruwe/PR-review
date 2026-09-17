"""The repo's own linter and type checker, run at both commits.

This is not our opinion about style. It is the standard the project already declared and
automated, applied to the change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

from prflagger.characterize.validate import base_checkout
from prflagger.models import Finding, Job
from prflagger.probes._support import SEVERITY, norm_for
from prflagger.sandbox.runner import lockfile_image_key, run_job

__all__ = ["lint_regression", "lint_errors"]

log = structlog.get_logger(__name__)

_MYPY_MARKER = "@@PRFLAGGER_MYPY@@"

# mypy appends its error code in brackets at end of line: "... [assignment]"
_MYPY_CODE = re.compile(r"\s*\[([a-z][a-z0-9-]*)\]\s*$")


def lint_regression(repo: Path, base: str, head: str) -> list[Finding]:
    """Run the repo's OWN ruff/mypy config at both commits. Finding per new error."""
    before = lint_errors(base_checkout(repo, base), base)
    after = lint_errors(base_checkout(repo, head), head)
    if before is None or after is None:
        log.warning("lint.unavailable", repo=str(repo))
        return []

    findings: list[Finding] = []
    for tool, norm_key in (("ruff", "lint_regression"), ("mypy", "lint_regression_types")):
        norm = norm_for(norm_key, repo)
        if norm is None:
            continue
        # Compare on (file, code, message), never line numbers: an unrelated insertion
        # shifts every line below it and would report the whole file as new errors.
        new = sorted(set(after.get(tool, ())) - set(before.get(tool, ())))
        for path, code, message in new:
            findings.append(
                Finding(
                    kind="lint_regression",
                    symbol=path,
                    what_changed=f"{tool} reports a new {code} in {path}: {message}",
                    how_we_know=f"{tool} at {head[:8]} using the repository's own config",
                    norm=norm,
                    confidence=1.0,
                    severity=SEVERITY["lint_regression"],
                )
            )
    log.info("lint_regression", findings=len(findings))
    return findings


def lint_errors(checkout: Path, commit: str) -> dict[str, list[tuple[str, str, str]]] | None:
    """(path, code, message) per tool. None when the probe could not run."""
    result = run_job(
        Job(
            repo_path=str(checkout.resolve()),
            commit=commit,
            image_key=lockfile_image_key(checkout),
            command=(
                "sh",
                "-c",
                # --no-pretty and --no-color-output change only how errors print, never
                # which rules run: the repo's own config still decides that. With pretty
                # on, mypy wraps long messages and every error parses as a fragment.
                "ruff check --output-format=json . 2>/dev/null; "
                f"echo '{_MYPY_MARKER}'; "
                "MYPY_CACHE_DIR=/tmp/.mypy mypy --no-error-summary --no-pretty "
                "--no-color-output . 2>&1 | head -300",
            ),
            timeout_s=900,
            memory_mb=2048,
        )
    )
    if not result.stdout.strip():
        return None
    ruff_text, _, mypy_text = result.stdout.partition(_MYPY_MARKER)
    return {"ruff": _parse_ruff(ruff_text), "mypy": _parse_mypy(mypy_text)}


def _parse_ruff(text: str) -> list[tuple[str, str, str]]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "[":
            continue
        try:
            payload, _ = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(payload, list):
            return [
                (
                    str(item.get("filename", "")).removeprefix("/src/"),
                    str(item.get("code") or ""),
                    str(item.get("message", "")),
                )
                for item in payload
                if isinstance(item, dict)
            ]
    return []


def _parse_mypy(text: str) -> list[tuple[str, str, str]]:
    errors: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if ": error:" not in line:
            continue
        location, _, rest = line.partition(": error:")
        path = location.split(":", 1)[0].strip().removeprefix("/src/")
        message = rest.strip()
        code = ""
        tagged = _MYPY_CODE.search(message)
        if tagged is not None:
            code = tagged.group(1)
            message = message[: tagged.start()].strip()
        errors.append((path, code, message))
    return errors
