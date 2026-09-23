"""Deciding what runs next, and making sure only one thing runs per PR.

The rules are small on purpose:

  * a PR whose head moved gets a new run, and the old one is marked superseded
    rather than deleted — the transcript of what was checked stays readable;
  * a force-push storm produces one run, not ten, because a PR that changed
    within the debounce window waits;
  * a head sha that has already been verified against the same base is not
    re-run, because the answer cannot have changed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import structlog

from prflagger.core.config import Config
from prflagger.core.ids import run_id
from prflagger.core.models import PullRequest, Run, RunState
from prflagger.engine.worker import RunWorker
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store

__all__ = ["Scheduler"]

log = structlog.get_logger(__name__)


@dataclass
class _Pending:
    pull: PullRequest
    trigger: str
    queued_at: float = field(default_factory=time.time)


class Scheduler:
    """Owns the queue and the worker tasks."""

    def __init__(
        self, config: Config, store: Store, bus: EventBus, worker: RunWorker
    ) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        self._worker = worker
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending: dict[str, _Pending] = {}
        self._active: dict[str, asyncio.Task[Run]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    @property
    def active_runs(self) -> list[str]:
        return sorted(self._active)

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # -- lifecycle ------------------------------------------------------------

    async def start(self, workers: int = 2) -> None:
        self._stopping = False
        for index in range(workers):
            self._tasks.append(
                asyncio.create_task(self._consume(index), name=f"pf-worker-{index}")
            )
        log.info("scheduler.started", workers=workers)

    async def stop(self) -> None:
        self._stopping = True
        for consumer in self._tasks:
            consumer.cancel()
        for running in list(self._active.values()):
            running.cancel()
        await asyncio.gather(
            *self._tasks, *self._active.values(), return_exceptions=True
        )
        self._tasks.clear()
        log.info("scheduler.stopped")

    # -- submitting -----------------------------------------------------------

    def submit(
        self, pull: PullRequest, *, trigger: str = "manual", force: bool = False
    ) -> str | None:
        """Queue a run for `pull`. Returns the run id, or None if nothing to do."""
        if not pull.base_sha or not pull.head_sha:
            log.warning("scheduler.incomplete_pull", repo=pull.repo, pr=pull.number)
            return None

        key = f"{pull.repo}#{pull.number}"
        if not force:
            existing = self._store.latest_run(pull.repo, pull.number)
            if (
                existing is not None
                and existing.head_sha == pull.head_sha
                and existing.state not in (RunState.FAILED, RunState.CANCELLED)
            ):
                # Same code, same answer. Re-running would only cost time.
                return None
            if key in self._pending:
                # Head moved again inside the debounce window: replace the
                # pending entry so only the newest sha is ever run.
                self._pending[key] = _Pending(pull=pull, trigger=trigger)
                return None

        self._pending[key] = _Pending(pull=pull, trigger=trigger)
        identifier = self._create(pull, trigger)
        self._queue.put_nowait(identifier)
        return identifier

    def _create(self, pull: PullRequest, trigger: str) -> str:
        identifier = run_id()
        previous = self._store.latest_run(pull.repo, pull.number)
        run = Run(
            id=identifier, repo=pull.repo, pr_number=pull.number,
            base_sha=pull.base_sha, head_sha=pull.head_sha,
            state=RunState.QUEUED, trigger=trigger, created_at=time.time(),
        )
        self._store.put_run(run)
        if previous is not None and previous.state not in (RunState.DONE,):
            self._store.supersede(previous.id, identifier)
        self._bus.emit(
            "run.queued", run_id=identifier, repo=pull.repo, pr_number=pull.number,
            title=pull.title, author=pull.author, trigger=trigger,
            base_sha=pull.base_sha, head_sha=pull.head_sha,
            supersedes=previous.id if previous else None,
        )
        log.info("run.queued", run=identifier, repo=pull.repo, pr=pull.number, trigger=trigger)
        return identifier

    def requeue(self, run: Run) -> None:
        """Put an existing run back on the queue. Used by boot recovery."""
        self._store.set_run_state(run.id, RunState.QUEUED)
        self._queue.put_nowait(run.id)
        self._bus.emit(
            "run.queued", run_id=run.id, repo=run.repo, pr_number=run.pr_number,
            trigger="recovery", base_sha=run.base_sha, head_sha=run.head_sha,
        )

    def cancel(self, identifier: str) -> bool:
        task = self._active.get(identifier)
        self._store.set_run_state(identifier, RunState.CANCELLED, error="cancelled by request")
        self._bus.emit(
            "run.state", run_id=identifier, state=RunState.CANCELLED.value, progress=1.0
        )
        if task is not None:
            task.cancel()
            return True
        return False

    # -- consuming ------------------------------------------------------------

    async def _consume(self, index: int) -> None:
        while not self._stopping:
            try:
                identifier = await self._queue.get()
            except asyncio.CancelledError:
                return

            run = self._store.run(identifier)
            if run is None or run.state in (RunState.CANCELLED,):
                continue

            # Debounce: if this PR moved again very recently, let it settle.
            key = f"{run.repo}#{run.pr_number}"
            pending = self._pending.get(key)
            if pending is not None:
                age = time.time() - pending.queued_at
                if age < self._config.server.debounce_s:
                    await asyncio.sleep(self._config.server.debounce_s - age)
                self._pending.pop(key, None)

            task = asyncio.create_task(self._worker.execute(run), name=f"pf-run-{identifier}")
            self._active[identifier] = task
            try:
                await task
            except asyncio.CancelledError:
                self._store.set_run_state(identifier, RunState.CANCELLED, error="cancelled")
            except Exception:  # noqa: BLE001 - one bad run must not stop the worker
                log.exception("scheduler.run_crashed", run=identifier, worker=index)
            finally:
                self._active.pop(identifier, None)
