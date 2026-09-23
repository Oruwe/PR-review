"""Deciding what runs next, and making sure only one thing runs per PR.

The rules are small on purpose:

  * a PR whose head moved gets a new run, and the old one is marked superseded
    rather than deleted — the transcript of what was checked stays readable;
  * a force-push storm produces one run, not ten, because a PR that changed
    within the debounce window waits;
  * a head sha that has already been verified against the same base is not
    re-run, because the answer cannot have changed.

Two more exist for volume. The queue is **bounded**: an unbounded queue under
sustained load is just a slower way to run out of memory, and a run admitted
now that will not start for six hours is a lie to whoever is waiting. And
dispatch is **round-robin across repositories**, so one repository with two
hundred open pull requests cannot starve every other one behind it — without
that, "watching five repositories" means watching the busiest and ignoring the
rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
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
    run_id: str = ""
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
        # One deque per repository, dispatched round-robin. A single queue would
        # let the busiest repository monopolise every worker.
        self._lanes: dict[str, deque[str]] = {}
        self._order: deque[str] = deque()
        self._waiting = asyncio.Event()
        self._depth = 0
        self._pending: dict[str, _Pending] = {}
        self._active: dict[str, asyncio.Task[Run]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self.rejected = 0

    @property
    def active_runs(self) -> list[str]:
        return sorted(self._active)

    @property
    def queue_depth(self) -> int:
        return self._depth

    @property
    def max_depth(self) -> int:
        return self._config.server.max_queue_depth

    def lane_depths(self) -> dict[str, int]:
        """How much work is waiting per repository. Shown on the dashboard."""
        return {repo: len(lane) for repo, lane in self._lanes.items() if lane}

    def _enqueue(self, repo: str, identifier: str) -> bool:
        """Admit work, or refuse it because the queue is already full.

        Refusing is deliberate. A run accepted into an unbounded queue under
        sustained load starts hours late, by which time its commit is stale and
        nobody is watching — and the memory it held meanwhile was real.
        """
        if self._depth >= self.max_depth:
            self.rejected += 1
            log.warning(
                "scheduler.queue_full", repo=repo, depth=self._depth,
                cap=self.max_depth, rejected_total=self.rejected,
            )
            self._bus.emit(
                "scheduler.rejected", repo=repo, run_id=identifier,
                depth=self._depth, cap=self.max_depth,
            )
            return False
        lane = self._lanes.setdefault(repo, deque())
        if not lane:
            self._order.append(repo)
        lane.append(identifier)
        self._depth += 1
        self._waiting.set()
        return True

    async def _next(self) -> str:
        """The next run, taking one repository's turn at a time."""
        while True:
            while self._order:
                repo = self._order[0]
                lane = self._lanes.get(repo)
                if not lane:
                    self._order.popleft()
                    continue
                identifier = lane.popleft()
                self._depth -= 1
                # Rotate: this repository goes to the back whether or not it has
                # more waiting, so the next dispatch belongs to someone else.
                self._order.rotate(-1)
                if not lane:
                    self._lanes.pop(repo, None)
                    with contextlib.suppress(ValueError):
                        self._order.remove(repo)
                return identifier
            self._waiting.clear()
            await self._waiting.wait()

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
            pending = self._pending.get(key)
            if pending is not None and pending.run_id:
                # The head moved again while a run for this PR was still queued.
                # Collapsing the storm into one run is right, but the queued run
                # has the *old* sha baked in — leaving it would verify the commit
                # that happened to be current when it was created. Point it at the
                # newest one instead.
                self._pending[key] = _Pending(
                    pull=pull, trigger=trigger, run_id=pending.run_id,
                    queued_at=pending.queued_at,
                )
                self._store.retarget(pending.run_id, pull.base_sha, pull.head_sha)
                self._bus.emit(
                    "run.retargeted", run_id=pending.run_id, repo=pull.repo,
                    pr_number=pull.number, head_sha=pull.head_sha,
                )
                log.info(
                    "run.retargeted", run=pending.run_id, repo=pull.repo,
                    pr=pull.number, head=pull.head_sha[:12],
                )
                return pending.run_id

        identifier = self._create(pull, trigger)
        if not self._enqueue(pull.repo, identifier):
            self._store.set_run_state(
                identifier, RunState.CANCELLED,
                error=(
                    f"queue is full ({self.max_depth} waiting); this run was not "
                    f"admitted. Raise server.max_queue_depth or add sandbox capacity."
                ),
            )
            return None
        self._pending[key] = _Pending(pull=pull, trigger=trigger, run_id=identifier)
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
        self._enqueue(run.repo, run.id)
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
                identifier = await self._next()
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
                # Re-read: the head may have been retargeted while this waited.
                run = self._store.run(identifier) or run

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
