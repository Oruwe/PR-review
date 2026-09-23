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
            # Only the container's own output. On a cold machine `docker run`
            # writes image-pull progress to stderr before the container starts,
            # and that chatter is the client talking, not the job.
            if stream == "stdout":
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
def test_a_container_that_is_slow_to_start_is_still_measured() -> None:
    """`docker inspect` answers once a container is created; its cgroup appears
    only once it starts. A sampler that looked once in that gap recorded nothing,
    which is how this failed on a loaded CI runner. Here the gap is forced: the
    container is created, then started a second and a half later.
    """
    import subprocess

    from prflagger.sandbox.stream import run_streaming

    name = f"pf-test-late-{int(time.time() * 1000)}"
    subprocess.run(  # noqa: S603
        ["docker", "create", "--rm", "--name", name, "--network=none", "--memory=512m",
         IMAGE, "python", "-c",
         "x = bytearray(150 * 1024 * 1024)\nimport time; time.sleep(2)\nprint(len(x))"],
        check=True, capture_output=True,
    )

    async def scenario() -> tuple[int | None, int]:
        result = await run_streaming(
            ["sh", "-c", f"sleep 1.5; exec docker start -a {name}"],
            timeout_s=90, container_name=name, sample_interval_s=0.3,
        )
        return result.peak_rss_mb, result.samples

    peak, samples = asyncio.run(scenario())
    assert samples > 0, "the sampler gave up before the container started"
    assert peak is not None and peak >= 140, f"a 150 MB allocation reported {peak} MB"


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


def test_a_report_on_one_line_reaches_the_parser_whole() -> None:
    """Reporters print their whole document on one line; only the display may cut it.

    pytest-json-report's line for pallets/click is over half a megabyte. It was cut
    at 20,000 characters, and past asyncio's 1 MB line limit silently dropped, so
    every real suite parsed to no per-test results and the behavioural comparison
    was skipped. Here the report is 3 MB, printed after more noise than the
    transcript is allowed to keep.
    """
    import sys

    from prflagger.lang.base import parse_tests
    from prflagger.sandbox.stream import StreamResult, run_streaming

    count = 12_000
    script = (
        "import json\n"
        "for i in range(200):\n    print('noise', i)\n"
        "tests = [{'nodeid': f'tests/test_x.py::test_case[{i}]', 'outcome': 'passed',"
        f" 'pad': 'x' * 200}} for i in range({count})]\n"
        "print(json.dumps({'tests': tests}))\n"
        f"print('{count} passed')\n"
    )

    def run(max_lines: int) -> tuple[StreamResult, list[str]]:
        shown: list[str] = []

        async def scenario() -> StreamResult:
            return await run_streaming(
                [sys.executable, "-c", script], timeout_s=60, max_lines=max_lines,
                on_line=lambda stream, text, offset: shown.append(text),
            )

        return asyncio.run(scenario()), shown

    for max_lines, report_shown in ((150, False), (10_000, True)):
        result, shown = run(max_lines)
        assert result.returncode == 0
        per_test = parse_tests("pytest-json", result.stdout)
        assert len(per_test) == count, f"{len(per_test)} of {count} tests reached the parser"
        assert not result.capture_full
        assert result.truncated is not report_shown
        assert all(len(line) < 20_100 for line in shown), "the display must still be cut"
        assert any("more characters]" in line for line in shown) is report_shown


@needs_docker
def test_a_large_suite_reports_every_test(pool: SandboxPool, tmp_path: Path) -> None:
    """The same, through a real container and the real pytest reporter."""
    repo, base, _ = build_repo(tmp_path / "repo")
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "checkout", "-q", base], check=True, capture_output=True
    )
    (repo / "tests" / "test_many.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('n', range(4000))\n"
        "def test_many(n):\n    assert n >= 0\n"
    )
    for argv in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t.invalid",
                                  "commit", "-qm", "many"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True)  # noqa: S603, S607
    commit = subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
        text=True,
    ).stdout.strip()

    spec = JobSpec(
        run_id="rMANY", job_id="rMANY-base", stage="base_run", repo_path=repo,
        commit=commit, toolchain=PYTHON, command=PYTHON.test.argv,
        timeout_s=180, memory_mb=512, package_roots=("shoplib",),
    )
    result = asyncio.run(pool.run(spec))
    assert result.outcome is Outcome.PASSED
    assert len(result.per_test) == 4005, f"only {len(result.per_test)} tests were parsed"


@needs_docker
def test_a_suites_declared_test_dependencies_are_installed(
    pool: SandboxPool, tmp_path: Path
) -> None:
    """Test-only dependencies live in a PEP 735 group, not in the project's own.

    Textualize/rich declares `attrs` there; with only the project installed its
    suite stopped at collection and nothing could be compared.
    """
    repo = tmp_path / "grouped"
    (repo / "grouped").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "grouped"\nversion = "0.1.0"\n\n'
        '[tool.setuptools]\npackages = ["grouped"]\n\n'
        '[dependency-groups]\ntest = ["six"]\n'
    )
    (repo / "grouped" / "__init__.py").write_text("VALUE = 1\n")
    (repo / "tests" / "test_grouped.py").write_text(
        "import six\n\nfrom grouped import VALUE\n\n\n"
        "def test_the_group_is_importable():\n    assert six.PY3 and VALUE == 1\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603, S607
    spec = JobSpec(
        run_id="rGROUP", job_id="rGROUP-base", stage="base_run", repo_path=repo,
        commit="0" * 40, toolchain=PYTHON, command=PYTHON.test.argv,
        timeout_s=180, memory_mb=512, package_roots=("grouped",),
    )
    result = asyncio.run(pool.run(spec))
    assert result.outcome is Outcome.PASSED, result.stdout[-2000:]
    assert result.per_test == {"tests/test_grouped.py::test_the_group_is_importable": "passed"}


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
