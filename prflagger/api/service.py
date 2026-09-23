"""The running service: every long-lived object, constructed once.

Having one place that owns the database, the bus, the pool, the worker, the
scheduler and the watcher is what makes `serve` a single process rather than a
set of components that each open their own connection and disagree about state.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from prflagger.brain.keeper import BrainKeeper
from prflagger.charter.keeper import CharterKeeper
from prflagger.core.config import Config, load
from prflagger.engine.janitor import disk_free_ratio, sweep
from prflagger.engine.notify import Notifier
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
    notifier: Notifier
    keeper: CharterKeeper
    brain: BrainKeeper
    started: bool = False
    janitor: asyncio.Task[None] | None = None
    learner: asyncio.Task[None] | None = None
    _background: set[asyncio.Task[object]] = field(default_factory=set)

    @classmethod
    def build(cls, config: Config | None = None, *, db_path: Path | None = None) -> Service:
        config = config or load()
        db = connect(db_path)
        store = Store(db)
        bus = EventBus(db)
        pool = SandboxPool(config, bus)
        notifier = Notifier(config, store, bus)
        worker = RunWorker(config, store, bus, pool, notifier)
        scheduler = Scheduler(config, store, bus, worker)
        github = GitHub()
        keeper = CharterKeeper(config, store, bus, notifier)
        watcher = Watcher(config, store, bus, scheduler, github, keeper=keeper)
        brain = BrainKeeper(config, store, bus)
        return cls(
            config=config, db=db, store=store, bus=bus, pool=pool, worker=worker,
            scheduler=scheduler, watcher=watcher, github=github, notifier=notifier,
            keeper=keeper, brain=brain,
        )

    def remember(self, slug: str) -> None:
        """Read a repository in the background if the service has no memory of it.

        A repository with no charter cannot have its pull requests judged against
        what it is for, and one with no norms cannot have them judged against what
        its reviewers enforce, so a new one is read immediately — charter first,
        then review history — rather than on the next branch movement or daily
        refresh.
        """
        repo = self.store.repo(slug)
        if repo is None or (repo.charter_sha and not self.brain.due(slug)):
            return
        task = asyncio.create_task(self._first_reading(slug), name=f"pf-remember-{slug}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _first_reading(self, slug: str) -> None:
        repo = self.store.repo(slug)
        if repo is not None and not repo.charter_sha:
            await self.keeper.refresh(slug, reason="first reading")
        await self.brain.refresh(slug, reason="first reading")

    async def _learn_periodically(self, every_s: float = 900.0) -> None:
        """Keep every repository's norms current. Cheap when nothing is due."""
        while True:
            await asyncio.sleep(every_s)
            for repo in self.store.repos():
                try:
                    await self.brain.refresh(repo.slug, reason="scheduled")
                except asyncio.CancelledError:
                    return
                except Exception:  # noqa: BLE001 - one repository must not stop the rest
                    log.exception("brain.scheduled_failed", repo=repo.slug)

    async def start(self, *, watch: bool = True) -> None:
        """Bring the engine up, repairing anything the last shutdown left behind."""
        self.bus.bind_loop(asyncio.get_running_loop())
        await self.scheduler.start(workers=self.pool.capacity)
        await recover(self.store, self.bus, self.scheduler)
        if watch:
            await self.watcher.start()
        self.janitor = asyncio.create_task(self._sweep_periodically(), name="pf-janitor")
        self.learner = asyncio.create_task(self._learn_periodically(), name="pf-brain")
        for repo in self.store.repos():
            self.remember(repo.slug)
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
        for task in list(self._background):
            task.cancel()
        for loop_task in (self.janitor, self.learner):
            if loop_task is not None:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
        self.brain.close()
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
            "queue_cap": self.scheduler.max_depth,
            "queue_by_repo": self.scheduler.lane_depths(),
            "queue_rejected": self.scheduler.rejected,
            "sandbox_capacity": self.pool.capacity,
            "event_head": self.bus.head(),
            "github_authenticated": self.github.authenticated,
            "github_rate_remaining": self.github.remaining,
            "last_poll_at": self.watcher.last_poll_at,
            "disk_free_ratio": round(disk_free_ratio(), 4),
            "open_major": len(self.store.notifications(open_only=True, min_level="major")),
            "webhook_configured": self.notifier.webhook_configured,
        }
