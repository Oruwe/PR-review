"""Measure a repo's real resource envelope instead of guessing it.

A fixed 120s timeout makes a slow repository look like an infinite loop. Run the suite
once unconstrained, measure what it actually needs, then derive the caps from that.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import structlog

from prflagger.sandbox.runner import (
    _docker,
    build_image,
    cache_root,
    lockfile_image_key,
)

__all__ = ["calibrate", "calibration_path"]

log = structlog.get_logger(__name__)

_MARKER = "__PRFLAGGER_CALIBRATION__"

# Runs inside the container: `ru_maxrss` of children is the peak RSS of the suite.
_PROBE = f"""\
import json, resource, subprocess, time
start = time.monotonic()
code = subprocess.call(["pytest", "-q", "-p", "no:cacheprovider"])
wall = time.monotonic() - start
rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print("{_MARKER}" + json.dumps({{"rss_kb": rss_kb, "wall_s": wall, "exit": code}}))
"""


def calibration_path() -> Path:
    return cache_root() / "calibration.json"


def calibrate(repo_path: Path, commit: str) -> tuple[int, int]:
    """Run the suite once unconstrained. Returns (memory_mb, timeout_s) =
    (2 x peak RSS, 3 x wall time). Persist to .cache/calibration.json."""
    key = f"{repo_path.resolve()}@{commit}"
    stored = _load().get(key)
    if stored is not None:
        return int(stored["memory_mb"]), int(stored["timeout_s"])

    image = build_image(repo_path, lockfile_image_key(repo_path))
    completed = _docker(
        [
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=512m",
            "--user",
            "1000:1000",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop=ALL",
            "-v",
            f"{repo_path}:/src:ro",
            image,
            "python",
            "-c",
            _PROBE,
        ],
        timeout_s=3600,
    )
    measured = _parse(completed.stdout)
    if measured is None:
        raise CalibrationError(
            f"calibration probe produced no measurement (exit {completed.returncode})",
            completed.stdout,
            completed.stderr,
        )

    memory_mb = _at_least_one(2 * measured["rss_kb"] / 1024)
    timeout_s = _at_least_one(3 * measured["wall_s"])
    _store(
        key,
        {
            "memory_mb": memory_mb,
            "timeout_s": timeout_s,
            "peak_rss_kb": measured["rss_kb"],
            "peak_rss_mb": _at_least_one(measured["rss_kb"] / 1024),
            "wall_s": measured["wall_s"],
            "commit": commit,
        },
    )
    log.info("calibrated", repo=key, memory_mb=memory_mb, timeout_s=timeout_s)
    return memory_mb, timeout_s


class CalibrationError(RuntimeError):
    """The unconstrained probe did not report a measurement."""

    def __init__(self, message: str, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


def _parse(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(_MARKER):
            try:
                parsed = json.loads(line[len(_MARKER) :])
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _at_least_one(value: float) -> int:
    return max(1, math.ceil(value))


def _load() -> dict[str, Any]:
    try:
        raw = calibration_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _store(key: str, value: dict[str, Any]) -> None:
    path = calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load()
    data[key] = value
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
