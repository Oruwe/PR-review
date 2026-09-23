"""Reading a running container's resource use straight from cgroupfs.

`docker stats` takes roughly a second to return because it samples CPU over an
interval, which means a job that finishes quickly reports nothing at all — and
`peak_rss_mb` silently going back to `None` is the v1 defect this whole sampler
exists to fix. Reading cgroupfs costs a few file reads, so it can be polled
several times a second and catches short jobs too.

Both cgroup layouts are handled, and every failure degrades to "no reading"
rather than raising: losing a sample must never fail a run.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ContainerProbe", "Reading"]

#: Where the host's cgroup tree is visible. When the service itself runs in a
#: container, its own /sys/fs/cgroup shows only its own cgroup (always, on cgroup
#: v1), so the host tree is mounted read-only elsewhere and named here.
_ROOT_ENV = "PRFLAGGER_CGROUP_ROOT"


def _cgroup_root() -> Path:
    return Path(os.environ.get(_ROOT_ENV, "").strip() or "/sys/fs/cgroup")

#: v2 first — on a hybrid host both exist and v2 is the accurate one.
_V2_DIRS = ("{cid}", "system.slice/docker-{cid}.scope", "docker/{cid}", "unified/docker/{cid}")
_V1_MEMORY = ("memory/docker/{cid}", "memory/system.slice/docker-{cid}.scope")
_V1_CPU = ("cpuacct/docker/{cid}", "cpuacct/system.slice/docker-{cid}.scope")
_V1_PIDS = ("pids/docker/{cid}",)


@dataclass(frozen=True)
class Reading:
    cpu_pct: float
    rss_mb: int
    pids: int


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip().split()[0]
    except (OSError, IndexError):
        return None
    if raw in ("max", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _first_dir(templates: tuple[str, ...], cid: str) -> Path | None:
    for template in templates:
        candidate = _cgroup_root() / template.format(cid=cid)
        if candidate.is_dir():
            return candidate
    return None


class ContainerProbe:
    """Locates a container's cgroup once, then reads it cheaply.

    CPU percentage is derived from the delta in cumulative CPU time between two
    readings, which is what `docker stats` reports and what the UI meter shows.
    """

    def __init__(self, container_id: str) -> None:
        self.cid = container_id
        self._v2 = _first_dir(_V2_DIRS, container_id)
        if self._v2 is not None and not (self._v2 / "memory.current").is_file():
            self._v2 = None
        self._memory = _first_dir(_V1_MEMORY, container_id)
        self._cpu = _first_dir(_V1_CPU, container_id)
        self._pids = _first_dir(_V1_PIDS, container_id)
        self._last_cpu_ns: int | None = None
        self._last_at: float | None = None

    @property
    def available(self) -> bool:
        return self._v2 is not None or self._memory is not None

    def read(self) -> Reading | None:
        rss = self._rss_bytes()
        if rss is None:
            return None
        return Reading(
            cpu_pct=self._cpu_pct(),
            rss_mb=int(rss / (1024 * 1024)),
            pids=self._pid_count(),
        )

    def peak_mb(self) -> int | None:
        """The high-water mark the kernel recorded, if it is still readable."""
        for path in self._peak_paths():
            value = _read_int(path)
            if value is not None:
                return int(value / (1024 * 1024))
        return None

    # -- internals ------------------------------------------------------------

    def _peak_paths(self) -> list[Path]:
        paths = []
        if self._v2 is not None:
            paths.append(self._v2 / "memory.peak")
        if self._memory is not None:
            paths.append(self._memory / "memory.max_usage_in_bytes")
        return paths

    def _rss_bytes(self) -> int | None:
        if self._v2 is not None:
            return _read_int(self._v2 / "memory.current")
        if self._memory is not None:
            return _read_int(self._memory / "memory.usage_in_bytes")
        return None

    def _pid_count(self) -> int:
        if self._v2 is not None:
            value = _read_int(self._v2 / "pids.current")
            if value is not None:
                return value
        if self._pids is not None:
            return _read_int(self._pids / "pids.current") or 0
        return 0

    def _cpu_ns(self) -> int | None:
        if self._v2 is not None:
            try:
                for line in (self._v2 / "cpu.stat").read_text(encoding="utf-8").splitlines():
                    if line.startswith("usage_usec"):
                        return int(line.split()[1]) * 1000
            except (OSError, IndexError, ValueError):
                return None
        if self._cpu is not None:
            return _read_int(self._cpu / "cpuacct.usage")
        return None

    def _cpu_pct(self) -> float:
        now = time.monotonic()
        current = self._cpu_ns()
        if current is None:
            return 0.0
        previous, previous_at = self._last_cpu_ns, self._last_at
        self._last_cpu_ns, self._last_at = current, now
        if previous is None or previous_at is None or now <= previous_at:
            return 0.0
        elapsed_ns = (now - previous_at) * 1e9
        return max(0.0, min(100.0 * (current - previous) / elapsed_ns, 1000.0))
