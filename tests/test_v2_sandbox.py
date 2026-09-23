"""The sandbox, against real containers.

CLAUDE.md: never mock Docker. Every test here starts a real container, because
the things worth asserting — that output arrives while the process runs, that a
timeout kills the container, that memory is measured — are exactly the things a
mock would assert into existence.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from prflagger.core.config import Config, SandboxConfig
from prflagger.core.models import Outcome
from prflagger.lang.python import PYTHON
from prflagger.sandbox.pool import JobSpec, SandboxPool, docker_argv
from prflagger.storage.db import Database
from prflagger.storage.events import EventBus
from tests.v2_fixtures import REGRESSION_TEST, build_repo

IMAGE = "python:3.11-slim"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(  # noqa: S603
        ["docker", "info"], capture_output=True, check=False
    ).returncode == 0


needs_docker = pytest.mark.skipif(
    not _docker_ready(), reason="a running Docker daemon is required; nothing here is mocked"
)


@pytest.fixture
def pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SandboxPool:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    config = Config(sandbox=SandboxConfig(default_timeout_s=120, sample_interval_s=0.3))
    database = Database(tmp_path / "test.db")
    bus = EventBus(database)
    return SandboxPool(config, bus)


def test_the_isolation_flags_are_exactly_what_is_promised(tmp_path: Path) -> None:
    """The UI shows this argv verbatim, so it has to be the real one."""
    spec = JobSpec(
        run_id="r", job_id="r-j", stage="head_run", repo_path=tmp_path, commit="c",
        toolchain=PYTHON, command=("pytest",), timeout_s=60, memory_mb=512,
    )
    argv = docker_argv(spec, "img:tag", "container")
    joined = " ".join(argv)
    for flag in (
        "--network=none", "--read-only", "--cap-drop=ALL", "--user 1000:1000",
        "--security-opt no-new-privileges", "--memory=512m", "--memory-swap=512m",
        "--pids-limit=256",
    ):
        assert flag in joined, f"{flag} missing from the sandbox invocation"
    assert f"{tmp_path}:/src:ro" in joined, "the checkout must be mounted read-only"
    # exec on the tmpfs: a suite that writes an executable under tmp_path and runs
    # it fails on a noexec tmpfs for that reason alone.
    assert "/tmp:rw,exec,size=256m" in joined
    assert argv[-3:] == ["timeout", "60", "pytest"]


@needs_docker
def test_output_arrives_while_the_process_is_still_running() -> None:
    """The property the live view depends on: streamed, not captured at the end."""
    from prflagger.sandbox.stream import run_streaming

    async def scenario() -> list[tuple[str, int]]:
        seen: list[tuple[str, int]] = []

        async def on_line(stream: str, text: str, offset_ms: int) -> None:
            seen.append((text, offset_ms))

        name = f"pf-test-stream-{int(time.time() * 1000)}"
        result = await run_streaming(
            ["docker", "run", "--rm", "--name", name, "--network=none", "--memory=256m",
             IMAGE, "python", "-c",
             "import time\nfor i in range(4):\n    print(i, flush=True); time.sleep(0.4)\n"],
            timeout_s=90, container_name=name, on_line=on_line, sample_interval_s=0.3,
        )
        assert result.returncode == 0
        return seen

    lines = asyncio.run(scenario())
    assert [text for text, _ in lines] == ["0", "1", "2", "3"]
    offsets = [offset for _, offset in lines]
    assert offsets == sorted(offsets), "offsets must be monotonic"
    # Captured-at-exit output would arrive with near-identical offsets.
    assert offsets[-1] - offsets[0] > 800, "output was batched, not streamed"


@needs_docker
def test_memory_is_measured_not_guessed() -> None:
    """v1 hardcoded `peak_rss_mb = None`, so `calibrate`'s contract was uncomputable."""
    from prflagger.sandbox.stream import run_streaming

    async def scenario() -> int | None:
        name = f"pf-test-mem-{int(time.time() * 1000)}"
        result = await run_streaming(
            ["docker", "run", "--rm", "--name", name, "--network=none", "--memory=512m",
             IMAGE, "python", "-c",
             "x = bytearray(150 * 1024 * 1024)\nimport time; time.sleep(2)\nprint(len(x))"],
            timeout_s=90, container_name=name, sample_interval_s=0.3,
        )
        return result.peak_rss_mb

    peak = asyncio.run(scenario())
    assert peak is not None, "no memory reading was taken"
    assert peak >= 140, f"a 150 MB allocation reported only {peak} MB"


@needs_docker
def test_a_runaway_job_times_out_and_leaves_nothing_behind() -> None:
    """SPEC.md § C1: a timeout is a finding, not an error — and must not leak."""
    from prflagger.sandbox.stream import run_streaming

    name = f"pf-test-timeout-{int(time.time() * 1000)}"

    async def scenario() -> tuple[int, float]:
        started = time.monotonic()
        result = await run_streaming(
            ["docker", "run", "--rm", "--name", name, "--network=none", "--memory=256m",
             IMAGE, "python", "-c", "while True: pass"],
            timeout_s=5, container_name=name, sample_interval_s=0.5,
        )
        return result.returncode, time.monotonic() - started

    code, elapsed = asyncio.run(scenario())
    assert code == 124
    assert elapsed < 5 + 10, f"took {elapsed:.1f}s; the outer bound is timeout_s + 10"
    survivors = subprocess.run(  # noqa: S603
        ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert not survivors, f"container {survivors} was left running"


@needs_docker
def test_a_real_suite_runs_and_identical_work_deduplicates(
    pool: SandboxPool, tmp_path: Path
) -> None:
    """End to end against a real repository, then again from the cache."""
    repo, base, _ = build_repo(tmp_path / "repo")
    # `build_repo` leaves the checkout at head, where the suite is meant to fail.
    # This assertion is about a healthy suite, so run it at base.
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "checkout", "-q", base], check=True, capture_output=True
    )
    spec = JobSpec(
        run_id="rTEST", job_id="rTEST-base", stage="base_run", repo_path=repo,
        commit=base, toolchain=PYTHON, command=PYTHON.test.argv,
        timeout_s=180, memory_mb=512, package_roots=("shoplib",),
    )

    result = asyncio.run(pool.run(spec))
    assert result.outcome is Outcome.PASSED
    assert len(result.per_test) == 5
    assert all(status == "passed" for status in result.per_test.values())
    assert result.peak_rss_mb is not None and result.peak_rss_mb > 0

    started = time.monotonic()
    again = asyncio.run(pool.run(spec))
    assert again.outcome is result.outcome
    assert time.monotonic() - started < 2.0, "an identical job re-ran Docker"


@needs_docker
def test_the_head_commit_fails_the_test_the_change_broke(
    pool: SandboxPool, tmp_path: Path
) -> None:
    """The fixture's whole point: a silent behaviour change shows up as a failure."""
    repo, base, head = build_repo(tmp_path / "repo")
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "checkout", "-q", head], check=True, capture_output=True
    )
    spec = JobSpec(
        run_id="rTEST2", job_id="rTEST2-head", stage="head_run", repo_path=repo,
        commit=head, toolchain=PYTHON, command=PYTHON.test.argv,
        timeout_s=180, memory_mb=512, package_roots=("shoplib",),
    )
    result = asyncio.run(pool.run(spec))
    assert result.outcome is Outcome.FAILED
    assert result.per_test[REGRESSION_TEST] == "failed"
