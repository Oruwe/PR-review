"""C1 acceptance.

The three criteria from SPEC.md § C1, against the configured target repo:
  (a) running the suite at HEAD returns PASSED with len(per_test) > 0
  (b) a job whose command is `python -c "while True: pass"` returns TIMEOUT within
      timeout_s + 10
  (c) calling run_job twice with an identical Job runs Docker once

Nothing here is mocked. The counter in (c) is a spy that delegates to the real Docker
call, which is the only way to count invocations without faking the thing under test.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from prflagger.models import Job, Outcome
from prflagger.sandbox import runner

SUITE_COMMAND = (
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "--json-report",
    "--json-report-file=/dev/stdout",
    "tests",
)


class DockerSpy:
    """Counts `docker run` invocations while still calling the real Docker."""

    def __init__(self) -> None:
        self.runs = 0
        self.all_calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = runner._docker

        def counting(argv: Sequence[str], *, timeout_s: float):  # type: ignore[no-untyped-def]
            self.all_calls += 1
            if argv and argv[0] == "run":
                self.runs += 1
            return real(argv, timeout_s=timeout_s)

        monkeypatch.setattr(runner, "_docker", counting)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[DockerSpy]:
    counter = DockerSpy()
    counter.install(monkeypatch)
    yield counter


# --------------------------------------------------------------------------------------
# (a) the suite at HEAD
# --------------------------------------------------------------------------------------


def test_suite_at_head_passes(head_worktree: Path, head_sha: str, image_key: str) -> None:
    job = Job(
        repo_path=str(head_worktree),
        commit=head_sha,
        image_key=image_key,
        command=SUITE_COMMAND,
        timeout_s=900,
        memory_mb=2048,
    )
    result = runner.run_job(job)

    failed = sorted(node for node, outcome in result.per_test.items() if outcome == "failed")
    assert result.outcome is Outcome.PASSED, f"{len(failed)} failed, e.g. {failed[:3]}"
    assert len(result.per_test) > 0


# --------------------------------------------------------------------------------------
# (b) an infinite loop is a TIMEOUT, not a hang
# --------------------------------------------------------------------------------------


def test_infinite_loop_returns_timeout(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    timeout_s = 10
    job = Job(
        repo_path=str(head_worktree),
        commit=head_sha,
        image_key=image_key,
        # Unique so this never answers from the result cache.
        command=("python", "-c", f"# {uuid.uuid4().hex}\nwhile True: pass"),
        timeout_s=timeout_s,
        memory_mb=512,
    )

    started = time.monotonic()
    result = runner.run_job(job)
    elapsed = time.monotonic() - started

    assert result.outcome is Outcome.TIMEOUT
    assert elapsed < timeout_s + 10, f"took {elapsed:.1f}s, budget {timeout_s + 10}s"


def test_timeout_is_returned_not_raised(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    # A timeout is a finding, not an exception. Re-running the cached job must also
    # hand back the typed outcome rather than blowing up.
    job = Job(
        repo_path=str(head_worktree),
        commit=head_sha,
        image_key=image_key,
        command=("python", "-c", "# fixed\nwhile True: pass"),
        timeout_s=5,
        memory_mb=512,
    )
    assert runner.run_job(job).outcome is Outcome.TIMEOUT
    assert runner.run_job(job).outcome is Outcome.TIMEOUT


# --------------------------------------------------------------------------------------
# (c) identical jobs deduplicate
# --------------------------------------------------------------------------------------


def test_identical_job_runs_docker_once(
    head_worktree: Path, head_sha: str, image_key: str, spy: DockerSpy
) -> None:
    marker = uuid.uuid4().hex
    job = Job(
        repo_path=str(head_worktree),
        commit=head_sha,
        image_key=image_key,
        command=("python", "-c", f"print({marker!r})"),
        timeout_s=60,
        memory_mb=512,
    )

    first = runner.run_job(job)
    assert spy.runs == 1, "the first call must actually run the container"

    second = runner.run_job(job)
    assert spy.runs == 1, "the second call must be served from cache, not Docker"

    assert first.outcome is Outcome.PASSED
    assert second.outcome is first.outcome
    assert second.per_test == first.per_test
    assert second.stdout == first.stdout
    assert marker in first.stdout


def test_a_different_field_is_a_different_job(
    head_worktree: Path, head_sha: str, image_key: str, spy: DockerSpy
) -> None:
    marker = uuid.uuid4().hex
    base = Job(
        repo_path=str(head_worktree),
        commit=head_sha,
        image_key=image_key,
        command=("python", "-c", f"print({marker!r})"),
        timeout_s=60,
        memory_mb=512,
    )
    runner.run_job(base)
    assert spy.runs == 1

    # Same command, different cap: a different job, so it must really run.
    runner.run_job(
        Job(
            repo_path=base.repo_path,
            commit=base.commit,
            image_key=base.image_key,
            command=base.command,
            timeout_s=base.timeout_s,
            memory_mb=600,
        )
    )
    assert spy.runs == 2


# --------------------------------------------------------------------------------------
# The mandated invocation
# --------------------------------------------------------------------------------------


def test_run_argv_is_the_invocation_the_spec_mandates(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    job = Job(
        repo_path=str(head_worktree),
        commit=head_sha,
        image_key=image_key,
        command=("pytest",),
        timeout_s=120,
        memory_mb=512,
    )
    argv = runner._run_argv(job, "some-image")

    assert argv[:1] == ["run"]
    for flag in (
        "--rm",
        "--network=none",
        "--memory=512m",
        "--memory-swap=512m",
        "--pids-limit=256",
        "--cpus=1",
        "--read-only",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop=ALL",
    ):
        assert flag in argv, f"missing {flag}"
    assert "/tmp:rw,exec,size=256m" in argv
    user_at = argv.index("--user")
    assert argv[user_at : user_at + 2] == ["--user", "1000:1000"]
    assert f"{job.repo_path}:/src:ro" in argv
    assert argv[-3:] == ["timeout", "120", "pytest"]


# --------------------------------------------------------------------------------------
# Calibration — measure the envelope instead of guessing it
# --------------------------------------------------------------------------------------


def test_calibrate_derives_caps_from_a_real_run(
    head_worktree: Path,
    head_sha: str,
    spy: DockerSpy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prflagger.sandbox import calibrate as calibration

    # A fresh cache root, so "the first calibration" is first by construction rather
    # than by whatever an earlier run happened to leave on disk. The image is keyed by
    # content and already built, so this does not trigger a rebuild.
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))

    memory_mb, timeout_s = calibration.calibrate(head_worktree, head_sha)

    assert memory_mb > 0
    assert timeout_s > 0
    assert spy.runs >= 1, "the first calibration must actually run the suite"

    stored = json.loads(calibration.calibration_path().read_text(encoding="utf-8"))
    entry = stored[f"{head_worktree.resolve()}@{head_sha}"]
    # The spec's formula, exactly: 2 x peak RSS, 3 x wall time.
    assert memory_mb == max(1, math.ceil(2 * entry["peak_rss_kb"] / 1024))
    assert timeout_s == max(1, math.ceil(3 * entry["wall_s"]))

    before = spy.runs
    assert calibration.calibrate(head_worktree, head_sha) == (memory_mb, timeout_s)
    assert spy.runs == before, "a second calibration must come from disk"


def test_calibrated_caps_actually_run_the_suite(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    from prflagger.sandbox import calibrate as calibration

    memory_mb, timeout_s = calibration.calibrate(head_worktree, head_sha)
    result = runner.run_job(
        Job(
            repo_path=str(head_worktree),
            commit=head_sha,
            image_key=image_key,
            command=SUITE_COMMAND,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
        )
    )
    # Caps derived from the repo's own measured envelope must not starve it.
    assert result.outcome is Outcome.PASSED
    assert len(result.per_test) > 0
