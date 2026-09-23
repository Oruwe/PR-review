"""Run a container and watch it happen.

v1 used `subprocess.run(capture_output=True)`, which returns everything at once
when the process is already over. That is fine for a CLI that prints a report at
the end and impossible for a UI that shows the run as it goes, so this module
replaces it.

Three things happen concurrently while a job runs:

  * both output streams are drained line by line and handed to `on_line`;
  * `docker stats` is sampled once a second and handed to `on_sample`, which is
    what finally makes `peak_rss_mb` a measurement rather than the `None` v1
    always returned;
  * a timer holds the outer bound, so a wedged docker client cannot hang the
    service even when the container's own `timeout` fails to fire.

Nothing here raises for job failure. A timeout and an OOM are findings.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from prflagger.sandbox.cgroup import ContainerProbe

__all__ = ["StreamResult", "run_streaming", "sample_container"]

log = structlog.get_logger(__name__)

LineSink = Callable[[str, str, int], Awaitable[None] | None]  # stream, text, offset_ms
SampleSink = Callable[[float, int, int, int], Awaitable[None] | None]  # cpu, rss, pids, offset

#: A single line longer than this is truncated. A test that dumps a megabyte on
#: one line should not be able to push everything else out of the transcript.
_MAX_LINE_CHARS = 20_000


@dataclass
class StreamResult:
    """What a streamed run produced."""

    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    peak_rss_mb: int | None = None
    peak_cpu_pct: float = 0.0
    samples: int = 0
    lines_emitted: int = 0
    truncated: bool = False
    timed_out: bool = False


@dataclass
class _Budget:
    """Bounds the transcript so a runaway suite cannot exhaust memory."""

    max_lines: int
    max_bytes: int
    lines: int = 0
    bytes_: int = 0
    tripped: bool = False

    def allow(self, text: str) -> bool:
        if self.tripped:
            return False
        self.lines += 1
        self.bytes_ += len(text)
        if self.lines > self.max_lines or self.bytes_ > self.max_bytes:
            self.tripped = True
            return False
        return True


async def _maybe_await(value: Awaitable[None] | None) -> None:
    if value is not None:
        await value


async def _drain(
    reader: asyncio.StreamReader,
    stream: str,
    started: float,
    on_line: LineSink | None,
    budget: _Budget,
    collected: list[str],
) -> None:
    """Read one stream to EOF, line by line, without buffering the whole thing."""
    while True:
        try:
            raw = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError):
            # A line longer than the stream limit: take what is there and carry on.
            raw = await reader.read(_MAX_LINE_CHARS)
        if not raw:
            return
        text = raw.decode("utf-8", "replace").rstrip("\n")
        if len(text) > _MAX_LINE_CHARS:
            text = text[:_MAX_LINE_CHARS] + f"… [{len(text) - _MAX_LINE_CHARS} more characters]"

        if not budget.allow(text):
            if budget.tripped and budget.lines == budget.max_lines + 1:
                notice = "… output limit reached; the rest of this run is not recorded"
                collected.append(notice)
                if on_line is not None:
                    await _maybe_await(on_line("meta", notice, _offset(started)))
            continue

        collected.append(text)
        if on_line is not None:
            await _maybe_await(on_line(stream, text, _offset(started)))


def _offset(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _container_id(name: str) -> str | None:
    """Resolve a container name to its full id, retrying while it starts."""
    for _ in range(20):
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "inspect", "-f", "{{.Id}}", name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            raw, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        except (OSError, TimeoutError):
            return None
        cid = raw.decode("utf-8", "replace").strip()
        if cid:
            return cid
        await asyncio.sleep(0.1)
    return None


async def sample_container(
    name: str,
    started: float,
    on_sample: SampleSink | None,
    interval_s: float,
    result: StreamResult,
) -> None:
    """Poll the container's resource use until cancelled.

    Readings come from cgroupfs rather than `docker stats`, which takes about a
    second to return and so misses short jobs entirely. When the cgroup cannot
    be located the sampler simply produces nothing — a missing reading is not
    worth failing a run over, and the report says memory was not measured rather
    than inventing a number.
    """
    cid = await _container_id(name)
    if cid is None:
        return
    probe = ContainerProbe(cid)
    if not probe.available:
        return

    # Poll faster than the UI needs so a job lasting under a second still
    # produces at least one reading.
    poll = max(0.1, min(interval_s, 0.25))
    emit_every = max(1, int(interval_s / poll))
    tick = 0
    try:
        while True:
            reading = probe.read()
            if reading is not None:
                result.samples += 1
                result.peak_cpu_pct = max(result.peak_cpu_pct, reading.cpu_pct)
                result.peak_rss_mb = max(result.peak_rss_mb or 0, reading.rss_mb)
                if on_sample is not None and tick % emit_every == 0:
                    await _maybe_await(
                        on_sample(
                            reading.cpu_pct, reading.rss_mb,
                            reading.pids, _offset(started),
                        )
                    )
            tick += 1
            await asyncio.sleep(poll)
    finally:
        # The kernel's own high-water mark beats anything polling observed.
        peak = probe.peak_mb()
        if peak is not None:
            result.peak_rss_mb = max(result.peak_rss_mb or 0, peak)


async def run_streaming(
    argv: list[str],
    *,
    timeout_s: float,
    container_name: str | None = None,
    on_line: LineSink | None = None,
    on_sample: SampleSink | None = None,
    sample_interval_s: float = 1.0,
    max_lines: int = 50_000,
    max_bytes: int = 8 * 1024 * 1024,
) -> StreamResult:
    """Run `argv`, streaming its output. Never raises for the job's own failure.

    `container_name` enables resource sampling; without it the run still streams,
    it just cannot report memory. Pass the same name the `docker run --name` flag
    carries.
    """
    started = time.monotonic()
    budget = _Budget(max_lines=max_lines, max_bytes=max_bytes)
    out_lines: list[str] = []
    err_lines: list[str] = []

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
    except OSError as error:
        return StreamResult(
            returncode=125,
            stdout="",
            stderr=f"could not start: {error}",
            duration_s=time.monotonic() - started,
        )

    result = StreamResult(returncode=0, stdout="", stderr="", duration_s=0.0)

    assert process.stdout is not None and process.stderr is not None  # noqa: S101 - PIPE above
    drains = [
        asyncio.create_task(
            _drain(process.stdout, "stdout", started, on_line, budget, out_lines)
        ),
        asyncio.create_task(
            _drain(process.stderr, "stderr", started, on_line, budget, err_lines)
        ),
    ]
    sampler = (
        asyncio.create_task(
            sample_container(container_name, started, on_sample, sample_interval_s, result)
        )
        if container_name
        else None
    )

    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        await _terminate(process, container_name)
    finally:
        if sampler is not None:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
        await asyncio.gather(*drains, return_exceptions=True)

    result.returncode = 124 if timed_out else (process.returncode or 0)
    result.stdout = "\n".join(out_lines)
    result.stderr = "\n".join(err_lines)
    result.duration_s = time.monotonic() - started
    result.lines_emitted = budget.lines
    result.truncated = budget.tripped
    result.timed_out = timed_out
    return result


async def _terminate(process: asyncio.subprocess.Process, container_name: str | None) -> None:
    """Stop the container, then the client. In that order, or the container leaks."""
    if container_name:
        with contextlib.suppress(OSError, TimeoutError):
            killer = await asyncio.create_subprocess_exec(
                "docker", "kill", container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=15.0)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=10.0)
