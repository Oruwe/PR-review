"""Keeps each repository's mined norms current while the service runs.

Once a day per repository (`[brain] refresh_hours`), and on the first reading of
a new one: read the merged pull requests GitHub has closed since the last
harvest, judge their review comments with the enforcement filter, and — only if
that produced a newly enforced comment — mine the norms again. A quiet
repository therefore costs one API request a day.

Declared norms are not built here. They come from the repository's files, so
`CharterKeeper` refreshes them whenever it re-reads those files.

Norms are per repository, like the charter: this reads and writes one
repository's review history at a time and never consults another's.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import structlog

from prflagger.brain.enforce import judge_comments
from prflagger.brain.harvest import HarvestUnavailable, fetch_reviews
from prflagger.brain.mine import lexical_vectoriser, mine_norms, semantic_vectoriser
from prflagger.core.config import Config
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store
from prflagger.vcs.github import GitHub

__all__ = ["BrainKeeper", "Learned", "NamerFactory"]

log = structlog.get_logger(__name__)

#: Given a repository, a function that names a cluster of comments and the id to
#: record for it — or None when no model is available or affordable.
NamerFactory = Callable[[str], "tuple[Callable[[Sequence[str]], str], str] | None"]


@dataclass(frozen=True)
class Learned:
    repo: str
    pulls_read: int
    comments_seen: int
    comments_enforced: int
    new_enforced: int
    norms: int
    clustered_by: str
    note: str


class BrainKeeper:
    """One harvest at a time, service-wide, so it never competes with the sandbox."""

    def __init__(
        self,
        config: Config,
        store: Store,
        bus: EventBus,
        github: GitHub | None = None,
        *,
        namer_factory: NamerFactory | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        # Its own client: a harvest's requests should not share conditional-request
        # state, or rate-limit accounting, with the watcher's polling.
        self._github = github or GitHub()
        self._namer_factory = namer_factory
        self._gate = asyncio.Semaphore(1)
        self._locks: dict[str, asyncio.Lock] = {}

    def close(self) -> None:
        self._github.close()

    def on_github(self, slug: str) -> bool:
        entry = self._config.repo(slug)
        return not entry.clone_url or entry.clone_url.startswith("https://github.com/")

    def due(self, slug: str) -> bool:
        state = self._store.brain_state(slug)
        if state is None or not state["built_at"]:
            return True
        return time.time() - float(state["built_at"]) >= self._config.brain.refresh_hours * 3600

    async def refresh(self, slug: str, *, reason: str = "requested", force: bool = False
                      ) -> Learned | None:
        if self._store.repo(slug) is None:
            return None
        if not force and not self.due(slug):
            return None
        lock = self._locks.setdefault(slug, asyncio.Lock())
        async with lock, self._gate:
            self._bus.emit("brain.building", repo=slug, reason=reason)
            try:
                learned = await asyncio.to_thread(functools.partial(self._learn, slug, force))
            except Exception as error:  # noqa: BLE001 - a failed harvest must not stop the service
                log.exception("brain.failed", repo=slug)
                self._store.put_brain_state(
                    slug, built_at=time.time(), note=f"last refresh failed: {str(error)[:200]}"
                )
                self._bus.emit("brain.failed", repo=slug, error=str(error)[:300])
                return None
        detail = {k: v for k, v in learned.__dict__.items() if k != "repo"}
        self._bus.emit("brain.updated", repo=slug, **detail)
        return learned

    def _learn(self, slug: str, force: bool) -> Learned:
        state = self._store.brain_state(slug) or {}
        brain = self._config.brain
        if not self.on_github(slug):
            note = ("review history is read from GitHub, and this repository is cloned "
                    "from elsewhere; only its declared norms apply")
            self._store.put_brain_state(slug, built_at=time.time(), note=note)
            seen, kept = self._store.review_counts(slug)
            return Learned(slug, 0, seen, kept, 0, len(self._store.norms(slug)), "", note)

        since = str(state.get("harvested_through") or "") or None
        limit = brain.incremental_limit if since else brain.harvest_limit
        notes: list[str] = []
        if not self._github.authenticated:
            limit = min(limit, brain.unauthenticated_limit)
            notes.append(
                f"no GitHub token, so at most {brain.unauthenticated_limit} pull requests "
                "are read per refresh; set GITHUB_TOKEN to read more"
            )

        try:
            records = fetch_reviews(self._github, slug, limit=limit, since=since)
        except HarvestUnavailable as error:
            notes.append(f"GitHub refused the harvest: {str(error)[:160]}")
            records = []
        new_enforced = self._store.put_review_comments(slug, judge_comments(records))
        through = max([since or "", *(str(r.get("updated_at") or "") for r in records)])
        seen, kept = self._store.review_counts(slug)

        mined = [n for n in self._store.norms(slug) if n.source == "mined"]
        clustered_by = str(state.get("clustered_by") or "")
        named_by = str(state.get("named_by") or "")
        if new_enforced or force or (kept and not state.get("built_at")):
            vectoriser = semantic_vectoriser() or lexical_vectoriser()
            naming = self._namer_factory(slug) if self._namer_factory else None
            mined = mine_norms(
                self._store.review_comments(slug, limit=brain.max_comments),
                vectoriser=vectoriser,
                namer=naming[0] if naming else None,
                namer_id=naming[1] if naming else "",
                min_support=brain.min_support,
                min_reviewers=brain.min_reviewers,
                max_comments=brain.max_comments,
            )
            self._store.replace_norms(slug, "mined", mined)
            clustered_by = vectoriser.name
            named_by = naming[1] if naming else "quote"
        if kept and not mined:
            notes.append(
                f"{kept} enforced comment(s), but no standard was raised by at least "
                f"{brain.min_support} comments from {brain.min_reviewers} or more reviewers"
            )

        note = "; ".join(notes)
        self._store.put_brain_state(
            slug, harvested_through=through,
            prs_seen=int(state.get("prs_seen") or 0) + len(records),
            built_at=time.time(), clustered_by=clustered_by, named_by=named_by, note=note,
        )
        log.info("brain.learned", repo=slug, pulls=len(records), seen=seen, enforced=kept,
                 new=new_enforced, norms=len(mined))
        return Learned(slug, len(records), seen, kept, new_enforced, len(mined),
                       clustered_by, note)
