"""Reclaiming disk.

Three things grow without bound on a busy service, and none of them is the
database:

  * a **worktree per commit**, which on a large repository is a full checkout;
  * an **image per distinct lockfile set**, which is hundreds of megabytes each
    and multiplies every time a dependency moves;
  * a **transcript per job**, kept so an old run stays readable.

Measured on a tiny demo repository, a handful of runs had already produced five
images. Nothing reclaimed any of it, so "always on" meant "fills the disk".

Everything here is conservative: anything a recent or in-flight run refers to is
kept, and a removal that fails is reported rather than retried into a loop. The
sweep is safe to run while the service is working.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from prflagger.core.config import Config, cache_root
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store

__all__ = ["Reclaimed", "disk_free_ratio", "sweep"]

log = structlog.get_logger(__name__)

#: Below this fraction of free disk, a new run is refused rather than started and
#: killed halfway. A run that cannot finish is worse than one that never began.
MIN_FREE_RATIO = 0.05


@dataclass
class Reclaimed:
    worktrees: int = 0
    images: int = 0
    transcripts: int = 0
    events: int = 0
    bytes_freed: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "worktrees": self.worktrees,
            "images": self.images,
            "transcripts": self.transcripts,
            "events": self.events,
            "mb_freed": round(self.bytes_freed / (1024 * 1024), 1),
            "failures": self.failures[:10],
        }


def disk_free_ratio(path: Path | None = None) -> float:
    """Free space as a fraction of the filesystem holding the cache."""
    try:
        usage = shutil.disk_usage(path or cache_root())
    except OSError:
        return 1.0
    return usage.free / usage.total if usage.total else 1.0


def _size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
    except OSError:
        pass
    return total


def sweep(
    store: Store,
    config: Config,
    bus: EventBus | None = None,
    *,
    keep_days: int | None = None,
    dry_run: bool = False,
) -> Reclaimed:
    """Reclaim what no recent or in-flight run needs. Never raises."""
    keep = keep_days if keep_days is not None else config.server.event_retention_days
    cutoff = time.time() - keep * 86400
    reclaimed = Reclaimed()

    terminal = ("done", "failed", "cancelled")
    live_runs = {
        run.id
        for run in store.runs(limit=20_000)
        if (run.created_at or 0) >= cutoff or run.state.value not in terminal
    }
    live_commits = {
        sha
        for run in store.runs(limit=20_000)
        if (run.created_at or 0) >= cutoff
        for sha in (run.base_sha, run.head_sha)
        if sha
    }

    _sweep_worktrees(live_commits, reclaimed, dry_run)
    _sweep_transcripts(live_runs, reclaimed, dry_run)
    _sweep_job_cache(cutoff, reclaimed, dry_run)
    _sweep_images(reclaimed, dry_run, keep_days=keep)

    if bus is not None and not dry_run:
        reclaimed.events = bus.prune(keep)
        bus.emit("janitor.swept", summary=reclaimed.as_dict())

    log.info("janitor.swept", dry_run=dry_run, **reclaimed.as_dict())
    return reclaimed


def _sweep_worktrees(live_commits: set[str], reclaimed: Reclaimed, dry_run: bool) -> None:
    """Drop checkouts for commits no recent run refers to.

    Removed through `git worktree remove` so the bare repository's own metadata
    stays consistent; deleting the directory alone leaves a dangling registration
    that makes the next `worktree add` for that commit fail.
    """
    root = cache_root() / "worktrees"
    if not root.is_dir():
        return
    for slug_dir in sorted(root.iterdir()):
        if not slug_dir.is_dir():
            continue
        bare = cache_root() / "repos" / f"{slug_dir.name}.git"
        for tree in sorted(slug_dir.iterdir()):
            if not tree.is_dir() or tree.name in live_commits:
                continue
            size = _size(tree)
            if dry_run:
                reclaimed.worktrees += 1
                reclaimed.bytes_freed += size
                continue
            removed = _git(bare, "worktree", "remove", "--force", str(tree))
            if removed.returncode != 0 and tree.exists():
                shutil.rmtree(tree, ignore_errors=True)
            if tree.exists():
                reclaimed.failures.append(f"worktree {tree.name[:12]}: {removed.stderr[:80]}")
                continue
            reclaimed.worktrees += 1
            reclaimed.bytes_freed += size
        if bare.is_dir() and not dry_run:
            _git(bare, "worktree", "prune")


def _sweep_transcripts(live_runs: set[str], reclaimed: Reclaimed, dry_run: bool) -> None:
    root = cache_root() / "runs"
    if not root.is_dir():
        return
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name in live_runs:
            continue
        size = _size(directory)
        if not dry_run:
            shutil.rmtree(directory, ignore_errors=True)
        reclaimed.transcripts += 1
        reclaimed.bytes_freed += size


def _sweep_job_cache(cutoff: float, reclaimed: Reclaimed, dry_run: bool) -> None:
    """Cached sandbox results older than the window. Re-running regenerates them."""
    root = cache_root() / "jobs"
    if not root.is_dir():
        return
    for entry in sorted(root.glob("*.json")):
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            size = entry.stat().st_size
        except OSError:
            continue
        if not dry_run:
            entry.unlink(missing_ok=True)
        reclaimed.bytes_freed += size


def _sweep_images(reclaimed: Reclaimed, dry_run: bool, *, keep_days: int) -> None:
    """Remove prflagger images nothing has used recently.

    Only images this system built are touched — they carry the `prflagger:`
    prefix. A base image someone else pulled is not ours to delete.
    """
    listed = _docker("images", "prflagger", "--format", "{{.ID}}\t{{.Tag}}\t{{.CreatedAt}}")
    if listed.returncode != 0:
        return
    for line in listed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        image_id, tag = parts[0], parts[1]
        if not _image_is_stale(image_id, keep_days):
            continue
        if dry_run:
            reclaimed.images += 1
            continue
        removed = _docker("rmi", f"prflagger:{tag}")
        if removed.returncode == 0:
            reclaimed.images += 1
        elif "being used" not in removed.stderr:
            reclaimed.failures.append(f"image {tag[:12]}: {removed.stderr.strip()[:80]}")


def _image_is_stale(image_id: str, keep_days: int) -> bool:
    """Stale when nothing has run it inside the window.

    `Metadata.LastTagTime` is not a use time, so this asks when the image was
    created and treats an old image with no recent container as reclaimable —
    rebuilding one is a few minutes, and keeping every one forever is gigabytes.
    """
    inspected = _docker("image", "inspect", image_id, "--format", "{{.Created}}")
    if inspected.returncode != 0:
        return False
    stamp = inspected.stdout.strip()[:19]
    try:
        created = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    return (time.time() - created) > keep_days * 86400


def _docker(*argv: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            ["docker", *argv], capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=list(argv), returncode=1, stdout="", stderr="")


def _git(bare: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            ["git", f"--git-dir={bare}", *argv],
            capture_output=True, text=True, check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=list(argv), returncode=1, stdout="", stderr="")
