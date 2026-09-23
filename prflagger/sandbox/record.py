"""Recording a sandbox run so it can be replayed line by line.

`runner.run_job` captures a run's output as one block, which is all the pipeline needs.
The interface needs one more fact about the same run: *when* each line arrived. So this
module performs the identical container invocation — `runner._run_argv`, unchanged — and
timestamps every line as the container emits it.

That makes the replay a replay. Nothing is interpolated, paced or invented: the offsets
are the offsets, measured from the moment the container started.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from prflagger.models import Job, Outcome, TestResult
from prflagger.sandbox import runner
from prflagger.sandbox.runner import ImageBuildError, build_image, cache_root

__all__ = ["RecordedRun", "record_run", "timeline_path"]

log = structlog.get_logger(__name__)

# Bumped when the recording format changes, so a cached timeline never outlives the
# shape its reader expects.
_RECORD_VERSION = 1

# One line of pytest output is never legitimately larger than this; a runaway
# repr would otherwise be read into memory in full.
_MAX_LINE_CHARS = 20_000


@dataclass(frozen=True)
class RecordedRun:
    """A run and its timeline. `result` is what `run_job` would have returned."""

    job: Job
    result: TestResult
    image: str
    exit_code: int | None
    argv: tuple[str, ...]
    lines: tuple[tuple[float, str], ...]


def timeline_path(job: Job) -> Path:
    return cache_root() / "records" / f"{job.idempotency_key}.json"


def record_run(job: Job) -> RecordedRun:
    """Run `job` in a container, keeping each output line's arrival time.

    Like `run_job`, this returns a typed outcome for every failure mode and raises for
    none of them: a timeout and an OOM are findings.
    """
    cached = _read(job)
    if cached is not None:
        log.debug("record.cache_hit", key=job.idempotency_key[:12])
        return cached

    try:
        image = build_image(Path(job.repo_path), job.image_key)
    except ImageBuildError as error:
        result = TestResult(
            outcome=Outcome.INSTALL_FAILED,
            per_test={},
            duration_s=0.0,
            peak_rss_mb=None,
            stdout=error.stdout,
            stderr=error.stderr,
        )
        recorded = RecordedRun(job, result, "", None, (), ())
        _write(recorded)
        return recorded

    argv = ["docker", *runner._run_argv(job, image)]
    lines: list[tuple[float, str]] = []
    out_chunks: list[str] = []
    err_chunks: list[str] = []
    started = time.monotonic()

    process = subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    readers = [
        threading.Thread(
            target=_drain,
            args=(stream, chunks, lines, started, stderr),
            daemon=True,
        )
        for stream, chunks, stderr in (
            (process.stdout, out_chunks, False),
            (process.stderr, err_chunks, True),
        )
        if stream is not None
    ]
    for reader in readers:
        reader.start()

    try:
        returncode = process.wait(timeout=job.timeout_s + runner._OUTER_GRACE_S)
    except subprocess.TimeoutExpired:
        # The container carries its own `timeout`; reaching here means docker itself
        # wedged. Killing it is the only way the caller gets an answer at all.
        process.kill()
        returncode = 124
    duration = time.monotonic() - started
    for reader in readers:
        reader.join(timeout=5.0)

    stdout = "".join(out_chunks)
    report = runner._extract_report(stdout)
    result = TestResult(
        outcome=runner._classify(returncode, report),
        per_test=runner._per_test(report),
        duration_s=duration,
        peak_rss_mb=None,
        stdout=stdout,
        stderr="".join(err_chunks),
    )
    recorded = RecordedRun(
        job=job,
        result=result,
        image=image,
        exit_code=returncode,
        argv=tuple(argv),
        lines=tuple(lines),
    )
    _write(recorded)
    log.info(
        "record.done",
        key=job.idempotency_key[:12],
        outcome=result.outcome.value,
        lines=len(lines),
        wall_s=round(duration, 2),
    )
    return recorded


def _drain(
    stream: object,
    chunks: list[str],
    lines: list[tuple[float, str]],
    started: float,
    stderr: bool,
) -> None:
    """Read one pipe, keeping both the raw text and each line's arrival offset."""
    for raw in stream:  # type: ignore[attr-defined]
        offset = time.monotonic() - started
        chunks.append(raw)
        text = raw.rstrip("\n")
        if len(text) > _MAX_LINE_CHARS:
            text = f"{text[:_MAX_LINE_CHARS]}… [{len(text) - _MAX_LINE_CHARS} more chars]"
        lines.append((offset, f"[stderr] {text}" if stderr else text))


def _read(job: Job) -> RecordedRun | None:
    try:
        entry = json.loads(timeline_path(job).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if entry.get("record_version") != _RECORD_VERSION:
        return None
    try:
        return RecordedRun(
            job=job,
            result=TestResult(
                outcome=Outcome(entry["outcome"]),
                per_test=dict(entry["per_test"]),
                duration_s=float(entry["duration_s"]),
                peak_rss_mb=entry["peak_rss_mb"],
                stdout=entry["stdout"],
                stderr=entry["stderr"],
            ),
            image=entry["image"],
            exit_code=entry["exit_code"],
            argv=tuple(entry["argv"]),
            lines=tuple((float(t), str(text)) for t, text in entry["lines"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write(recorded: RecordedRun) -> None:
    path = timeline_path(recorded.job)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_version": _RECORD_VERSION,
        "outcome": recorded.result.outcome.value,
        "per_test": recorded.result.per_test,
        "duration_s": recorded.result.duration_s,
        "peak_rss_mb": recorded.result.peak_rss_mb,
        "stdout": recorded.result.stdout,
        "stderr": recorded.result.stderr,
        "image": recorded.image,
        "exit_code": recorded.exit_code,
        "argv": list(recorded.argv),
        "lines": [[offset, text] for offset, text in recorded.lines],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)
