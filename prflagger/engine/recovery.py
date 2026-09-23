"""Putting things right after a restart.

A service that runs continuously will be killed mid-run — by a deploy, a reboot,
an OOM. Two things are then wrong: runs are stuck in a state they will never
leave, and containers are still executing with nobody reading their output.

Both are repaired at boot. Runs left in flight go back on the queue; containers
we started are killed by label. Nothing here guesses: a run's state is in the
database and a container carries `prflagger=1`.
"""

from __future__ import annotations

import asyncio

import structlog

from prflagger.core.models import Run
from prflagger.engine.scheduler import Scheduler
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store

__all__ = ["recover"]

log = structlog.get_logger(__name__)


async def recover(store: Store, bus: EventBus, scheduler: Scheduler) -> list[Run]:
    """Requeue interrupted runs and kill orphaned containers. Returns the runs."""
    orphans = await _kill_orphans()
    if orphans:
        log.info("recovery.containers_killed", count=orphans)

    stranded = store.unfinished_runs()
    for run in stranded:
        log.info(
            "recovery.requeued", run=run.id, repo=run.repo, pr=run.pr_number,
            was=run.state.value,
        )
        bus.emit(
            "run.recovered", run_id=run.id, repo=run.repo, pr_number=run.pr_number,
            interrupted_at=run.state.value,
        )
        scheduler.requeue(run)

    if stranded or orphans:
        bus.emit("service.recovered", runs=len(stranded), containers=orphans)
    return stranded


async def _kill_orphans() -> int:
    """Kill every container this service started that is somehow still running."""
    try:
        lister = await asyncio.create_subprocess_exec(
            "docker", "ps", "-q", "--filter", "label=prflagger=1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await asyncio.wait_for(lister.communicate(), timeout=30.0)
    except (OSError, TimeoutError):
        return 0

    ids = [line for line in raw.decode("utf-8", "replace").split() if line]
    if not ids:
        return 0
    try:
        killer = await asyncio.create_subprocess_exec(
            "docker", "kill", *ids,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=60.0)
    except (OSError, TimeoutError):
        return 0
    return len(ids)
