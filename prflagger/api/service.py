"""The running service: every long-lived object, constructed once.

Having one place that owns the database, the bus, the pool, the worker, the
scheduler and the watcher is what makes `serve` a single process rather than a
set of components that each open their own connection and disagree about state.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

import structlog

from prflagger.core.config import Config, load
from prflagger.engine.janitor import disk_free_ratio, sweep
from prflagger.engine.recovery import recover
from prflagger.engine.scheduler import Scheduler
from prflagger.engine.watcher import Watcher
from prflagger.engine.worker import RunWorker
from prflagger.sandbox.pool import SandboxPool
from prflagger.storage.db import Database, connect
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store
from prflagger.vcs.github import GitHub

__all__ = ["Service"]

log = structlog.get_logger(__name__)


@dataclass
class Service:
    """Everything with a lifetime longer than one request."""

    config: Config
    db: Database
    store: Store
    bus: EventBus
    pool: SandboxPool
    worker: RunWorker
    scheduler: Scheduler
    watcher: Watcher
    github: GitHub
    started: bool = False
    janitor: asyncio.Task[None] | None = None

    @classmethod
    def build(cls, config: Config | None = None, *, db_path: Path | None = None) -> Service:
        config = config or load()
        db = connect(db_path)
        store = Store(db)
        bus = EventBus(db)
        pool = SandboxPool(config, bus)
        worker = RunWorker(config, store, bus, pool)
        scheduler = Scheduler(config, store, bus, worker)
        github = GitHub()
        watcher = Watcher(config, store, bus, scheduler, github)
        return cls(
            config=config, db=db, store=store, bus=bus, pool=pool, worker=worker,
            scheduler=scheduler, watcher=watcher, github=github,
        )

    async def start(self, *, watch: bool = True) -> None:
        """Bring the engine up, repairing anything the last shutdown left behind."""
        self.bus.bind_loop(asyncio.get_running_loop())
        await self.scheduler.start(workers=self.pool.capacity)
        await recover(self.store, self.bus, self.scheduler)
        if watch:
            await self.watcher.start()
        self.janitor = asyncio.create_task(self._sweep_periodically(), name="pf-janitor")
        self.started = True
        self.bus.emit("service.started", workers=self.pool.capacity, watching=watch)
        log.info("service.started", workers=self.pool.capacity, watching=watch)

    async def _sweep_periodically(self, every_s: float = 3600.0) -> None:
        """Reclaim disk hourly. A service that never tidies fills the disk."""
        while True:
            await asyncio.sleep(every_s)
            try:
                await asyncio.to_thread(sweep, self.store, self.config, self.bus)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - a failed sweep must not stop the service
                log.exception("janitor.failed")

    async def stop(self) -> None:
        if self.janitor is not None:
            self.janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.janitor
        await self.watcher.stop()
        await self.scheduler.stop()
        self.started = False
        log.info("service.stopped")

    def health(self) -> dict[str, object]:
        return {
            "started": self.started,
            "repos": len(self.store.repos()),
            "active_runs": self.scheduler.active_runs,
            "queue_depth": self.scheduler.queue_depth,
            "sandbox_capacity": self.pool.capacity,
            "event_head": self.bus.head(),
            "github_authenticated": self.github.authenticated,
            "github_rate_remaining": self.github.remaining,
            "last_poll_at": self.watcher.last_poll_at,
            "disk_free_ratio": round(disk_free_ratio(), 4),
        }
