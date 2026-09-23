"""Scheduling under load: bounded admission and no repository starving another."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from prflagger.core.config import Config, SandboxConfig, ServerConfig
from prflagger.core.models import PullRequest, Repo, RunState
from prflagger.engine.scheduler import Scheduler
from prflagger.engine.worker import RunWorker
from prflagger.sandbox.pool import SandboxPool
from prflagger.storage.db import Database
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store


def _scheduler(tmp_path: Path, *, cap: int = 200) -> tuple[Scheduler, Store]:
    config = Config(
        server=ServerConfig(max_queue_depth=cap, debounce_s=0),
        sandbox=SandboxConfig(max_concurrent=1),
    )
    database = Database(tmp_path / "s.db")
    store = Store(database)
    bus = EventBus(database)
    for slug in ("busy/repo", "quiet/repo", "third/repo"):
        store.put_repo(Repo(slug=slug, added_at=time.time()))
    pool = SandboxPool(config, bus)
    return Scheduler(config, store, bus, RunWorker(config, store, bus, pool)), store


def _pull(repo: str, number: int) -> PullRequest:
    return PullRequest(
        repo=repo, number=number, title=f"pr {number}", body="", author="u",
        base_sha=f"base{number}", head_sha=f"head{repo}{number}",
        state="open", updated_at="now",
    )


def test_one_busy_repo_cannot_starve_the_others(tmp_path: Path) -> None:
    """Round-robin dispatch, or watching five repos means watching the busiest.

    The guarantee is that no repository gets a second turn before every
    repository with work waiting has had a first. A single FIFO queue would hand
    out twenty runs from the busy repository before the others saw one.
    """

    async def scenario() -> list[str]:
        scheduler, _ = _scheduler(tmp_path)
        for number in range(20):
            scheduler.submit(_pull("busy/repo", number))
        for number in range(20):
            scheduler.submit(_pull("quiet/repo", number))
        for number in range(20):
            scheduler.submit(_pull("third/repo", number))

        order: list[str] = []
        for _ in range(9):
            identifier = await scheduler._next()  # noqa: SLF001
            run = scheduler._store.run(identifier)  # noqa: SLF001
            assert run is not None
            order.append(run.repo)
        return order

    order = asyncio.run(scenario())
    # Three repositories, nine dispatches: three clean rounds, each covering all three.
    for start in (0, 3, 6):
        window = order[start : start + 3]
        assert sorted(window) == ["busy/repo", "quiet/repo", "third/repo"], (
            f"round {start // 3 + 1} was not fair: {window} (full order {order})"
        )


def test_a_drained_repo_yields_its_turn(tmp_path: Path) -> None:
    """Fairness must not mean idling. Once others are empty, the rest is dispatched."""

    async def scenario() -> list[str]:
        scheduler, _ = _scheduler(tmp_path)
        for number in range(5):
            scheduler.submit(_pull("busy/repo", number))
        scheduler.submit(_pull("quiet/repo", 1))
        order: list[str] = []
        for _ in range(6):
            identifier = await scheduler._next()  # noqa: SLF001
            run = scheduler._store.run(identifier)  # noqa: SLF001
            assert run is not None
            order.append(run.repo)
        return order

    order = asyncio.run(scenario())
    assert order.count("quiet/repo") == 1
    assert order.count("busy/repo") == 5, "capacity went unused once the other lane emptied"
    assert order.index("quiet/repo") <= 1, "the quiet repo waited behind the whole backlog"


def test_the_queue_is_bounded_and_says_so(tmp_path: Path) -> None:
    """Admission is refused loudly rather than accepted into an unbounded backlog."""

    async def scenario() -> tuple[int, int, list[str | None]]:
        scheduler, store = _scheduler(tmp_path, cap=5)
        accepted: list[str | None] = []
        for number in range(12):
            accepted.append(scheduler.submit(_pull("busy/repo", number)))
        refused = [
            run for run in store.runs(repo="busy/repo", limit=50)
            if run.state is RunState.CANCELLED
        ]
        return scheduler.queue_depth, scheduler.rejected, [r.error for r in refused[:1]]

    depth, rejected, errors = asyncio.run(scenario())
    assert depth == 5, f"queue grew past its cap: {depth}"
    assert rejected == 7
    assert errors and "queue is full" in (errors[0] or "")
    assert errors and "max_queue_depth" in (errors[0] or ""), "the message must name the fix"


def test_a_rejected_run_does_not_linger_as_queued(tmp_path: Path) -> None:
    """A run that was never admitted must not look like one that is waiting."""

    async def scenario() -> list[RunState]:
        scheduler, store = _scheduler(tmp_path, cap=2)
        for number in range(5):
            scheduler.submit(_pull("busy/repo", number))
        return [run.state for run in store.runs(repo="busy/repo", limit=50)]

    states = asyncio.run(scenario())
    assert states.count(RunState.QUEUED) == 2
    assert states.count(RunState.CANCELLED) == 3
    assert not any(s is RunState.PREPARING for s in states)


def test_recovery_respects_the_cap(tmp_path: Path) -> None:
    """Boot recovery uses the same admission path, not a back door around it."""

    async def scenario() -> int:
        scheduler, store = _scheduler(tmp_path, cap=3)
        for number in range(6):
            scheduler.submit(_pull("busy/repo", number))
        depth_after_submit = scheduler.queue_depth
        # Requeue is what recovery calls; it must not push past the cap either.
        run = store.runs(repo="busy/repo", limit=1)[0]
        scheduler.requeue(replace(run, state=RunState.QUEUED))
        return max(depth_after_submit, scheduler.queue_depth)

    assert asyncio.run(scenario()) == 3
