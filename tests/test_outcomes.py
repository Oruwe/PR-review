"""The full outcome mapping from SPEC.md § C1.

    exit 0 -> PASSED; exit 1 with a parseable report -> FAILED; exit 124 -> TIMEOUT;
    exit 137 -> OOM; pytest exit 2 -> COLLECTION_ERROR; image build failure ->
    INSTALL_FAILED.

Only PASSED and TIMEOUT were covered by C1's own acceptance block. The rest are the
outcomes that carry meaning downstream — an OOM and a collection error are findings, not
errors to swallow — so each one is produced here by a real container doing the real
thing, not by asserting on a table.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from prflagger.models import Job, Outcome
from prflagger.sandbox import runner


def _job(worktree: Path, sha: str, key: str, command: tuple[str, ...], **kwargs: int) -> Job:
    return Job(
        repo_path=str(worktree),
        commit=sha,
        image_key=key,
        command=command,
        timeout_s=int(kwargs.get("timeout_s", 60)),
        memory_mb=int(kwargs.get("memory_mb", 512)),
    )


# --------------------------------------------------------------------------------------
# The mapping as a table
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, Outcome.PASSED),
        (1, Outcome.FAILED),
        (2, Outcome.COLLECTION_ERROR),
        (124, Outcome.TIMEOUT),
        (125, Outcome.INSTALL_FAILED),  # docker itself could not start the container
        (137, Outcome.OOM),
    ],
)
def test_exit_codes_map_as_the_spec_says(code: int, expected: Outcome) -> None:
    assert runner._classify(code, None) is expected


def test_an_unmapped_failure_is_still_a_failure_not_a_crash() -> None:
    assert runner._classify(3, None) is Outcome.FAILED
    assert runner._classify(255, None) is Outcome.FAILED


# --------------------------------------------------------------------------------------
# Each outcome, produced by a real container
# --------------------------------------------------------------------------------------


def test_a_passing_command_is_passed(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    result = runner.run_job(
        _job(head_worktree, head_sha, image_key, ("python", "-c", "pass"))
    )
    assert result.outcome is Outcome.PASSED


def test_a_failing_command_is_failed(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    result = runner.run_job(
        _job(head_worktree, head_sha, image_key, ("python", "-c", "raise SystemExit(1)"))
    )
    assert result.outcome is Outcome.FAILED


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("import", "import nope_prflagger_missing\n\n\ndef test_x():\n    pass\n"),
        ("syntax", "def test_bad(:\n    pass\n"),
    ],
)
def test_a_collection_error_is_its_own_outcome(
    label: str, source: str, head_worktree: Path, head_sha: str, image_key: str
) -> None:
    """A PR that does not import is often the most important result, so it must not be
    reported as an ordinary test failure.

    A missing file is a pytest *usage* error (exit 4); a module that cannot be collected
    is exit 2. Only the second is what this outcome means.
    """
    staged = head_worktree / ".prflagger" / f"test_collect_{label}.py"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(source, encoding="utf-8")
    try:
        result = runner.run_job(
            _job(
                head_worktree,
                head_sha,
                image_key,
                (
                    "pytest", "-q", "-p", "no:cacheprovider",
                    f".prflagger/{staged.name}",
                ),
            )
        )
    finally:
        staged.unlink(missing_ok=True)

    assert result.outcome is Outcome.COLLECTION_ERROR


def test_exhausting_memory_is_oom_and_is_returned_not_raised(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    """A memory regression is a finding. The run must come back as a value."""
    marker = uuid.uuid4().hex
    result = runner.run_job(
        _job(
            head_worktree,
            head_sha,
            image_key,
            ("python", "-c", f"# {marker}\nx=[]\nwhile True: x.append(' '*10_000_000)"),
            memory_mb=64,
            timeout_s=120,
        )
    )
    assert result.outcome is Outcome.OOM
    assert result.duration_s > 0


def test_an_unbuildable_repo_is_install_failed(tmp_path: Path) -> None:
    """Cannot verify this repo — say so explicitly rather than skipping it."""
    broken = tmp_path / "broken"
    broken.mkdir()
    # A pyproject that no backend can build: the image build fails, which the spec maps
    # to INSTALL_FAILED rather than an exception.
    (broken / "pyproject.toml").write_text(
        '[project]\nname = "broken"\nversion = "0.1"\n'
        '[build-system]\nrequires = ["nonexistent-backend-prflagger"]\n'
        'build-backend = "nonexistent_backend_prflagger"\n',
        encoding="utf-8",
    )
    result = runner.run_job(
        Job(
            repo_path=str(broken),
            commit="0" * 40,
            image_key=runner.lockfile_image_key(broken),
            command=("python", "-c", "pass"),
            timeout_s=60,
            memory_mb=512,
        )
    )
    assert result.outcome is Outcome.INSTALL_FAILED
    assert result.stderr or result.stdout, "the reason must be recorded, not discarded"


def test_no_job_failure_ever_raises(
    head_worktree: Path, head_sha: str, image_key: str
) -> None:
    """Every failure mode above returns a value. Nothing propagates an exception."""
    commands = (
        ("python", "-c", "raise SystemExit(1)"),
        ("python", "-c", "import sys; sys.exit(2)"),
        ("does-not-exist-binary",),
        ("python", "-c", "raise RuntimeError('boom')"),
    )
    for command in commands:
        result = runner.run_job(_job(head_worktree, head_sha, image_key, command))
        assert isinstance(result.outcome, Outcome)


# --------------------------------------------------------------------------------------
# Result cache fidelity
# --------------------------------------------------------------------------------------


def test_a_cached_result_round_trips_every_field(
    head_worktree: Path, head_sha: str, image_key: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    job = _job(
        head_worktree, head_sha, image_key,
        ("python", "-c", f"print({uuid.uuid4().hex!r})"),
    )
    first = runner.run_job(job)
    second = runner.run_job(job)

    assert second.outcome is first.outcome
    assert second.per_test == first.per_test
    assert second.stdout == first.stdout
    assert second.stderr == first.stderr
    assert second.peak_rss_mb == first.peak_rss_mb


def test_a_result_from_a_different_invocation_is_not_reused(
    head_worktree: Path, head_sha: str, image_key: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job.idempotency_key covers the job, not the flags that ran it."""
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    job = _job(head_worktree, head_sha, image_key, ("python", "-c", "pass"))
    runner.run_job(job)

    stored = runner._result_path(job)
    import json

    entry = json.loads(stored.read_text(encoding="utf-8"))
    entry["invocation_version"] = -1
    stored.write_text(json.dumps(entry), encoding="utf-8")

    assert runner._read_result(job) is None, "a stale invocation must miss, not mislead"


def test_a_truncated_cache_entry_is_a_miss_not_a_crash(
    head_worktree: Path, head_sha: str, image_key: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    job = _job(head_worktree, head_sha, image_key, ("python", "-c", "pass"))
    path = runner._result_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"outcome": "pas', encoding="utf-8")

    assert runner._read_result(job) is None
