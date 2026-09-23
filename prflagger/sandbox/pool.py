"""The bounded pool that actually runs jobs.

One place decides how a container is launched, so the isolation flags exist in
exactly one list and the UI can show that list verbatim. Concurrency is an
`asyncio.Semaphore`, which is all SPEC.md permits and all this workload needs —
the bottleneck is the container, and two of them saturate a four-core box.

Every job emits events as it goes: `job.started` carrying the exact argv,
`log.line` per line, `job.sample` per resource reading, `job.finished` with the
typed outcome. The live UI is those events; the stored transcript is the same
events replayed. There is no second code path for "watching" a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from prflagger.core.config import Config, cache_root
from prflagger.core.errors import ImageBuildError
from prflagger.core.models import Job, Outcome, TestResult
from prflagger.engine.janitor import MIN_FREE_RATIO, disk_free_ratio
from prflagger.lang.base import Toolchain, parse_tests
from prflagger.sandbox.images import build_image, image_key_for
from prflagger.sandbox.stream import run_streaming
from prflagger.storage.events import EventBus
from prflagger.vcs.worktrees import ensure_readable

__all__ = ["JobSpec", "SandboxPool", "docker_argv"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class JobSpec:
    """One sandboxed command, with everything needed to run and record it."""

    run_id: str
    job_id: str
    stage: str  # "base_run" | "head_run" | "lint" | "coverage" | ...
    repo_path: Path  # the worktree, mounted read-only
    commit: str
    toolchain: Toolchain
    command: tuple[str, ...]
    timeout_s: int = 300
    memory_mb: int = 1024
    package_roots: tuple[str, ...] = ()
    system_binaries: tuple[str, ...] = ()
    report_format: str = ""
    #: Exit codes that mean the command ran and produced a result. Empty means
    #: pytest's convention, which is the default for a test job.
    ok_codes: tuple[int, ...] = ()

    def as_job(self, image_key: str) -> Job:
        """The v1 `Job`, whose `idempotency_key` still keys the result cache."""
        return Job(
            repo_path=str(self.repo_path),
            commit=self.commit,
            image_key=image_key,
            command=self.command,
            timeout_s=self.timeout_s,
            memory_mb=self.memory_mb,
        )


def docker_argv(spec: JobSpec, image: str, container_name: str) -> list[str]:
    """The exact invocation. SPEC.md § C1 pins these flags; the UI shows them.

    `exec` on the tmpfs is required: Docker mounts a tmpfs `noexec` by default,
    and a suite whose tests write an executable file under `tmp_path` and run it
    fails for that reason alone. Isolation is unchanged otherwise — no network,
    no capabilities, non-root, read-only root, capped memory and pids.
    """
    return [
        "docker", "run", "--rm",
        "--name", container_name,
        "--label", "prflagger=1",
        "--label", f"prflagger.run={spec.run_id}",
        "--network=none",
        f"--memory={spec.memory_mb}m",
        f"--memory-swap={spec.memory_mb}m",
        "--pids-limit=256",
        "--cpus=1",
        "--read-only",
        "--tmpfs", "/tmp:rw,exec,size=256m",
        "--user", "1000:1000",
        "--security-opt", "no-new-privileges",
        "--cap-drop=ALL",
        "-v", f"{spec.repo_path}:/src:ro",
        image,
        "timeout", str(spec.timeout_s),
        *spec.command,
    ]


class SandboxPool:
    """Runs jobs, bounded, streaming, and recorded."""

    def __init__(self, config: Config, bus: EventBus) -> None:
        self._config = config
        self._bus = bus
        self._semaphore = asyncio.Semaphore(config.sandbox.resolved_concurrency())
        self._image_locks: dict[str, asyncio.Lock] = {}
        self._cancelled: set[str] = set()

    @property
    def capacity(self) -> int:
        return self._config.sandbox.resolved_concurrency()

    def cancel(self, run_id: str) -> None:
        """Mark a run cancelled. In-flight containers are killed by name."""
        self._cancelled.add(run_id)

    # -- the main entry point -------------------------------------------------

    async def run(self, spec: JobSpec) -> TestResult:
        """Execute `spec`. Never raises for the job's own failure."""
        image_key = await asyncio.to_thread(
            image_key_for, spec.repo_path, spec.toolchain,
            system_binaries=spec.system_binaries,
        )
        job = spec.as_job(image_key)

        cached = _read_cached(job)
        if cached is not None:
            log.debug("job.cache_hit", job=spec.job_id, key=job.idempotency_key[:12])
            self._bus.emit(
                "job.finished", run_id=spec.run_id, job_id=spec.job_id, stage=spec.stage,
                outcome=cached.outcome.value, tests=len(cached.per_test),
                duration_s=cached.duration_s, peak_rss_mb=cached.peak_rss_mb, cached=True,
            )
            return cached

        async with self._semaphore:
            if spec.run_id in self._cancelled:
                return _result(Outcome.FAILED, stderr="run cancelled before this job started")

            free = disk_free_ratio()
            if free < MIN_FREE_RATIO:
                # Starting here would fail partway with a confusing error. Say
                # what is actually wrong instead.
                message = (
                    f"refusing to start: only {free * 100:.1f}% of the disk is free "
                    f"(minimum {MIN_FREE_RATIO * 100:.0f}%). Run `prflagger gc`."
                )
                log.error("sandbox.disk_exhausted", free_ratio=round(free, 4))
                self._bus.emit(
                    "job.finished", run_id=spec.run_id, job_id=spec.job_id,
                    stage=spec.stage, outcome=Outcome.INSTALL_FAILED.value,
                    tests=0, duration_s=0.0, error=message,
                )
                return _result(Outcome.INSTALL_FAILED, stderr=message)

            return await self._execute(spec, job, image_key)

    async def _execute(self, spec: JobSpec, job: Job, image_key: str) -> TestResult:
        self._bus.emit(
            "job.building", run_id=spec.run_id, job_id=spec.job_id, stage=spec.stage,
            pack=spec.toolchain.id,
        )
        try:
            image = await self._build(spec, image_key)
        except ImageBuildError as error:
            # Cannot verify this repo — say so explicitly rather than skipping silently.
            result = _result(Outcome.INSTALL_FAILED, stdout=error.stdout, stderr=error.stderr)
            _write_cached(job, result)
            self._bus.emit(
                "job.finished", run_id=spec.run_id, job_id=spec.job_id, stage=spec.stage,
                outcome=result.outcome.value, tests=0, duration_s=0.0,
                error=error.stderr[-2000:],
            )
            return result

        # The container runs unprivileged; a tree it cannot read fails on an
        # arbitrary file rather than as a permissions error.
        await asyncio.to_thread(ensure_readable, spec.repo_path)

        container = f"pf-{spec.job_id}-{int(time.time())}"[:60]
        argv = docker_argv(spec, image, container)
        log_path = cache_root() / "runs" / spec.run_id / f"{spec.job_id}.ndjson"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        self._bus.emit(
            "job.started", run_id=spec.run_id, job_id=spec.job_id, stage=spec.stage,
            argv=argv, image=image, container=container, memory_mb=spec.memory_mb,
            timeout_s=spec.timeout_s, log_path=str(log_path),
        )

        sink = _LogSink(log_path)
        counter = {"seq": 0}

        async def on_line(stream: str, text: str, offset_ms: int) -> None:
            counter["seq"] += 1
            # The file is the durable record and is written first; the publish
            # is live-only. A log line never becomes a database row — see
            # `EventBus.publish`.
            sink.write(stream, counter["seq"], offset_ms, text)
            self._bus.publish(
                "log.line", run_id=spec.run_id, job_id=spec.job_id,
                stream=stream, line_seq=counter["seq"], offset_ms=offset_ms, text=text,
            )

        async def on_sample(cpu: float, rss: int, pids: int, offset_ms: int) -> None:
            self._bus.emit(
                "job.sample", run_id=spec.run_id, job_id=spec.job_id,
                cpu_pct=cpu, rss_mb=rss, pids=pids, offset_ms=offset_ms,
                memory_mb=spec.memory_mb,
            )

        watcher = asyncio.create_task(self._watch_cancel(spec.run_id, container))
        try:
            stream = await run_streaming(
                argv,
                timeout_s=spec.timeout_s + self._config.sandbox.outer_grace_s,
                container_name=container,
                on_line=on_line,
                on_sample=on_sample,
                sample_interval_s=self._config.sandbox.sample_interval_s,
                max_lines=self._config.sandbox.max_log_lines,
                max_bytes=self._config.sandbox.max_log_bytes,
            )
        finally:
            watcher.cancel()
            sink.close()

        per_test = parse_tests(spec.report_format or spec.toolchain.test.report_format,
                               stream.stdout)
        result = TestResult(
            outcome=_classify(stream.returncode, spec.ok_codes),
            per_test=per_test,
            duration_s=stream.duration_s,
            peak_rss_mb=stream.peak_rss_mb,
            stdout=stream.stdout,
            stderr=stream.stderr,
        )
        _write_cached(job, result)
        self._bus.emit(
            "job.finished", run_id=spec.run_id, job_id=spec.job_id, stage=spec.stage,
            outcome=result.outcome.value, tests=len(per_test),
            passed=sum(1 for v in per_test.values() if v == "passed"),
            failed=sum(1 for v in per_test.values() if v == "failed"),
            duration_s=round(stream.duration_s, 2), peak_rss_mb=stream.peak_rss_mb,
            truncated=stream.truncated, capture_full=stream.capture_full,
            exit_code=stream.returncode,
        )
        if stream.capture_full:
            log.warning("job.capture_full", job=spec.job_id,
                        note="output past the capture limit was not parsed")
        log.info(
            "job.done", job=spec.job_id, outcome=result.outcome.value,
            tests=len(per_test), rss_mb=stream.peak_rss_mb,
        )
        return result

    async def _build(self, spec: JobSpec, image_key: str) -> str:
        """Build the image, serialised per key so concurrent jobs build once."""
        lock = self._image_locks.setdefault(image_key, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(
                build_image,
                spec.repo_path,
                spec.toolchain,
                image_key=image_key,
                package_roots=spec.package_roots,
                system_binaries=spec.system_binaries,
            )

    async def _watch_cancel(self, run_id: str, container: str) -> None:
        """Kill the container if the run is cancelled while it is in flight."""
        while True:
            await asyncio.sleep(1.0)
            if run_id not in self._cancelled:
                continue
            with_kill = await asyncio.create_subprocess_exec(
                "docker", "kill", container,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await with_kill.wait()
            return


def _classify(returncode: int, ok_codes: tuple[int, ...] = ()) -> Outcome:
    """SPEC.md § C1's mapping. A timeout and an OOM are results, not errors.

    `ok_codes` lets a non-test job say which exits mean "ran and found things".
    A linter exiting 1 has done its job; calling that a collection error would
    discard a perfectly good result.
    """
    if returncode == 124:
        return Outcome.TIMEOUT
    if returncode == 137:
        return Outcome.OOM
    if returncode == 125:
        return Outcome.INSTALL_FAILED  # docker itself could not start the container
    if returncode == 0:
        return Outcome.PASSED
    if ok_codes:
        return Outcome.PASSED if returncode in ok_codes else Outcome.FAILED
    if returncode == 2:
        return Outcome.COLLECTION_ERROR
    return Outcome.FAILED


def _result(outcome: Outcome, *, stdout: str = "", stderr: str = "") -> TestResult:
    return TestResult(
        outcome=outcome, per_test={}, duration_s=0.0, peak_rss_mb=None,
        stdout=stdout, stderr=stderr,
    )


class _LogSink:
    """Appends the transcript as newline-delimited JSON, flushed per line.

    Flushing every line is deliberate: if the service is killed mid-run, the
    transcript up to that moment must still be on disk, because that is what the
    UI replays when the run is requeued.
    """

    def __init__(self, path: Path) -> None:
        self._handle = path.open("a", encoding="utf-8")

    def write(self, stream: str, seq: int, offset_ms: int, text: str) -> None:
        self._handle.write(
            json.dumps(
                {"stream": stream, "seq": seq, "offset_ms": offset_ms, "text": text},
                ensure_ascii=False,
            )
            + "\n"
        )
        self._handle.flush()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._handle.close()


# ----------------------------------------------------------------------------------
# Result cache — identical work deduplicates, so re-runs during iteration are free
# ----------------------------------------------------------------------------------

_INVOCATION_VERSION = 3


def _cache_path(job: Job) -> Path:
    return cache_root() / "jobs" / f"{job.idempotency_key}.json"


def _read_cached(job: Job) -> TestResult | None:
    try:
        entry = json.loads(_cache_path(job).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if entry.get("invocation_version") != _INVOCATION_VERSION:
        return None  # produced by a different invocation; re-run rather than mislead
    try:
        return TestResult(
            outcome=Outcome(entry["outcome"]),
            per_test=dict(entry["per_test"]),
            duration_s=float(entry["duration_s"]),
            peak_rss_mb=entry["peak_rss_mb"],
            stdout=entry["stdout"],
            stderr=entry["stderr"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write_cached(job: Job, result: TestResult) -> None:
    path = _cache_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "outcome": result.outcome.value,
        "per_test": result.per_test,
        "duration_s": result.duration_s,
        "peak_rss_mb": result.peak_rss_mb,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "invocation_version": _INVOCATION_VERSION,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)  # atomic: a reader never sees a half-written entry
