"""Watching GitHub for work.

Polling, not webhooks, as the default: a webhook needs a public address, and
this is meant to run on a laptop or a small box behind NAT. Conditional requests
make polling nearly free — an unchanged PR list costs no rate-limit quota at all
— so a two-minute interval is affordable even on an unauthenticated token.

The webhook path exists too (`api.routes_hooks`), and both funnel into the same
`Scheduler.submit`, so there is one decision point rather than two.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import replace

import structlog

from prflagger.core.config import Config
from prflagger.core.errors import RepoUnavailable
from prflagger.engine.scheduler import Scheduler
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store
from prflagger.vcs.github import GitHub

__all__ = ["Watcher"]

log = structlog.get_logger(__name__)


class Watcher:
    """Polls each watched repo and hands new or moved PRs to the scheduler."""

    def __init__(
        self,
        config: Config,
        store: Store,
        bus: EventBus,
        scheduler: Scheduler,
        github: GitHub | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        self._scheduler = scheduler
        self._github = github or GitHub()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.last_poll_at: float | None = None
        self.polls = 0

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="pf-watcher")
        log.info(
            "watcher.started",
            interval_s=self._config.server.poll_interval_s,
            authenticated=self._github.authenticated,
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._github.close()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - a bad poll must not end the watcher
                log.exception("watcher.poll_failed")
            await asyncio.sleep(self._config.server.poll_interval_s)

    async def poll_once(self) -> int:
        """One sweep over every watched repo. Returns how many runs it queued."""
        queued = 0
        for repo in self._store.repos():
            entry = self._config.repo(repo.slug)
            if not entry.watch:
                continue
            queued += await self._poll_repo(repo.slug, entry.max_prs)
        self.polls += 1
        self.last_poll_at = time.time()
        self._bus.emit(
            "watcher.polled", queued=queued, repos=len(self._store.repos()),
            rate_remaining=self._github.remaining,
        )
        return queued

    async def _poll_repo(self, slug: str, max_prs: int) -> int:
        try:
            pulls = await asyncio.to_thread(self._github.open_pulls, slug, limit=max_prs)
        except RepoUnavailable as error:
            log.warning("watcher.repo_unavailable", repo=slug, error=str(error)[:200])
            self._bus.emit("watcher.error", repo=slug, error=str(error)[:300])
            return 0

        queued = 0
        seen = set()
        for pull in pulls:
            seen.add(pull.number)
            known = self._store.pull(slug, pull.number)
            self._store.put_pull(pull)
            if known is None or known.head_sha != pull.head_sha:
                self._bus.emit(
                    "pull.changed", repo=slug, pr_number=pull.number, title=pull.title,
                    head_sha=pull.head_sha, author=pull.author,
                    reason="new" if known is None else "head moved",
                )
                if self._scheduler.submit(pull, trigger="watcher") is not None:
                    queued += 1

        # A PR that vanished from the open list has closed; record that rather
        # than leaving a stale open row the queue view would keep showing.
        for stored in self._store.pulls(slug, state="open"):
            if stored.number not in seen:
                self._store.put_pull(replace(stored, state="closed"))
        return queued
