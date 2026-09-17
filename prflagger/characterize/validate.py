"""The validation loop: execution is the filter.

Generated tests are untrusted. A test that fails against the unchanged code described
behaviour the model imagined, so it is discarded and regenerated with the real failure
fed back. Only tests that pass on base are allowed to say anything about head.

This is the project's own thesis turned on its own agent: do not trust the model's
output, run it.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from prflagger.characterize.generate import RETRY_SUFFIX, split_test_functions
from prflagger.llm import complete
from prflagger.models import Job, Outcome, TestResult
from prflagger.providers import DEFAULT_MODEL
from prflagger.sandbox.calibrate import calibrate
from prflagger.sandbox.runner import cache_root, lockfile_image_key, run_job

__all__ = ["base_checkout", "metrics_path", "run_tests_on", "validate_on_base"]

log = structlog.get_logger(__name__)

_STAGING = ".prflagger"


def metrics_path() -> Path:
    return cache_root() / "metrics.jsonl"


def validate_on_base(
    tests: list[str],
    repo: Path,
    base_sha: str,
    *,
    max_attempts: int = 3,
) -> tuple[list[str], float]:
    """Run tests against base. Keep only those that PASS. Regenerate failures,
    feeding the failure output back. Returns (surviving_tests, discard_rate)."""
    started = time.monotonic()
    checkout = base_checkout(repo, base_sha)

    survivors: list[str] = []
    pending = list(tests)
    seen = len(pending)
    attempts_used = 0

    for attempt in range(1, max_attempts + 1):
        if not pending:
            break
        attempts_used = attempt
        outcomes, output = run_tests_on(pending, checkout, base_sha, label=f"a{attempt}")
        passed = [s for i, s in enumerate(pending) if outcomes.get(i) == "passed"]
        failed = [s for i, s in enumerate(pending) if outcomes.get(i) != "passed"]
        survivors.extend(passed)

        if not failed or attempt == max_attempts:
            break
        # Feed the real failure back: the model asserted behaviour the code does not have.
        pending = _regenerate(failed, output)
        seen += len(pending)

    # A test that does not reproduce twice on one commit is not evidence of anything.
    survivors = _drop_flaky(survivors, checkout, base_sha)

    discard_rate = 0.0 if seen == 0 else 1.0 - (len(survivors) / seen)
    _record(
        {
            "stage": "validate_on_base",
            "commit": base_sha,
            "tests_generated": seen,
            "tests_survived": len(survivors),
            "tests_discarded": seen - len(survivors),
            "discard_rate": round(discard_rate, 4),
            "attempts_used": attempts_used,
            "wall_s": round(time.monotonic() - started, 3),
        }
    )
    log.info(
        "validated_on_base",
        generated=seen,
        survived=len(survivors),
        discard_rate=round(discard_rate, 3),
    )
    return survivors, discard_rate


def _drop_flaky(survivors: list[str], checkout: Path, base_sha: str) -> list[str]:
    if not survivors:
        return survivors
    outcomes, _ = run_tests_on(survivors, checkout, base_sha, label="confirm")
    return [s for i, s in enumerate(survivors) if outcomes.get(i) == "passed"]


def _regenerate(failed: Sequence[str], failure_output: str) -> list[str]:
    regenerated: list[str] = []
    for source in failed:
        prompt = (
            "Here is a pytest test written to record existing behaviour.\n\n"
            f"{source}\n"
            + RETRY_SUFFIX.format(failure_output=failure_output[-4000:])
            + "\nReturn only Python code, no prose, no markdown fences.\n"
        )
        regenerated.extend(
            split_test_functions(complete(prompt, model=DEFAULT_MODEL, max_tokens=4096))
        )
    return regenerated


def run_tests_on(
    tests: Sequence[str], checkout: Path, commit: str, *, label: str = "run"
) -> tuple[dict[int, str], str]:
    """Run each test in the sandbox. Returns index -> pytest outcome, and the output.

    Tests are renamed so an index survives name collisions between independently
    generated sources.
    """
    if not tests:
        return {}, ""

    module, index_by_node = _assemble(tests)
    digest = hashlib.sha256(module.encode("utf-8")).hexdigest()[:16]
    relative = f"{_STAGING}/char_{label}_{digest}.py"
    staged = checkout / relative
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(module, encoding="utf-8")

    try:
        memory_mb, timeout_s = calibrate(checkout, commit)
        result = run_job(
            Job(
                repo_path=str(checkout.resolve()),
                commit=commit,
                image_key=lockfile_image_key(checkout),
                command=(
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--json-report",
                    "--json-report-file=/dev/stdout",
                    relative,
                ),
                timeout_s=timeout_s,
                memory_mb=memory_mb,
            )
        )
    finally:
        staged.unlink(missing_ok=True)

    return _outcomes(result, index_by_node), _failure_output(result)


def _assemble(tests: Sequence[str]) -> tuple[str, dict[str, int]]:
    """One module, one uniquely named test per source."""
    chunks: list[str] = []
    index_by_node: dict[str, int] = {}
    for index, source in enumerate(tests):
        renamed, name = _rename_first_test(source, index)
        chunks.append(renamed)
        index_by_node[name] = index
    return "\n\n".join(chunks) + "\n", index_by_node


def _rename_first_test(source: str, index: int) -> tuple[str, str]:
    name = f"test_char_{index}"
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Unparseable source cannot pass; keep it in the module so it is counted as
        # failing rather than silently dropped.
        return f"def {name}():\n    raise SyntaxError('generated source did not parse')\n", name
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test_"
        ):
            node.name = name
            break
    else:
        return f"def {name}():\n    raise AssertionError('no test function in source')\n", name
    return ast.unparse(tree), name


def _outcomes(result: TestResult, index_by_node: dict[str, int]) -> dict[int, str]:
    outcomes: dict[int, str] = {}
    for nodeid, outcome in result.per_test.items():
        function = nodeid.rsplit("::", 1)[-1].split("[", 1)[0]
        index = index_by_node.get(function)
        if index is not None:
            outcomes[index] = outcome
    if result.outcome in {Outcome.COLLECTION_ERROR, Outcome.TIMEOUT, Outcome.OOM}:
        # Nothing ran, or the run died: no test may claim to have passed.
        for index in index_by_node.values():
            outcomes.setdefault(index, result.outcome.value)
    return outcomes


def _failure_output(result: TestResult) -> str:
    text = result.stdout
    marker = text.find("=================================== FAILURES")
    return text[marker:] if marker != -1 else text[-4000:]


def base_checkout(repo: Path, base_sha: str) -> Path:
    """A checkout of `base_sha`. Reuses `repo` when it is already there."""
    head = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if head == base_sha:
        return repo

    target = cache_root() / "worktrees" / "checkouts" / base_sha
    if target.is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "worktree", "add", "--detach", "--quiet",
         str(target), base_sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 and not target.is_dir():
        # Fall back to a plain copy rather than failing: the caller needs a tree.
        shutil.copytree(repo, target, ignore=shutil.ignore_patterns(".git"))
    return target


def _record(entry: dict[str, Any]) -> None:
    path = metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": time.time(), **entry}) + "\n")
