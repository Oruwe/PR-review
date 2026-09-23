"""Keeping a repository's memory current.

The keeper owns one job: when a repository's default branch moves, rebuild what
the system knows about it — the atlas and the charter together, from one
checkout — compare the new charter with the last one, record how far it moved,
and announce it at the volume the movement deserves.

A repository's memory is rebuilt only from that repository's own checkout and
compared only with its own previous charter. There is no path by which one
repository's charter can inform another's.
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from typing import Any

import structlog

from prflagger.atlas.build import build_atlas
from prflagger.brain.declared import declared_norms
from prflagger.charter.drift import compare, evidence_lines, headline
from prflagger.charter.extract import extract_charter
from prflagger.core.config import Config
from prflagger.core.models import Charter, CharterDrift, Symbol
from prflagger.engine.notify import Notifier
from prflagger.lang.detect import for_repo
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store
from prflagger.vcs.git import remote_head
from prflagger.vcs.worktrees import worktree_for

__all__ = ["CharterKeeper", "Refreshed"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Refreshed:
    charter: Charter
    drift: CharterDrift | None  # None for a repository's first charter
    atlas_modules: int


class CharterKeeper:
    """Rebuilds and compares repository memory. One rebuild at a time, service-wide.

    Rebuilding parses a whole repository, which is CPU-bound and, on a large one,
    takes a while. Serialising rebuilds keeps them from starving the sandbox pool
    of the cores it needs to run pull requests.
    """

    def __init__(self, config: Config, store: Store, bus: EventBus, notifier: Notifier) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        self._notifier = notifier
        self._gate = asyncio.Semaphore(1)
        self._locks: dict[str, asyncio.Lock] = {}

    def _source(self, slug: str) -> str:
        entry = self._config.repo(slug)
        return entry.clone_url or f"https://github.com/{slug}"

    async def check(self, slug: str) -> Refreshed | None:
        """Has the default branch moved? If so, and refresh is enabled, rebuild."""
        repo = self._store.repo(slug)
        if repo is None:
            return None
        head = await asyncio.to_thread(remote_head, self._source(slug), repo.default_branch)
        if head is None or head == repo.branch_head:
            return None
        self._store.set_branch_head(slug, head)
        self._bus.emit("repo.branch_moved", repo=slug, head=head, previous=repo.branch_head)
        if not self._config.charter.refresh_on_branch_move:
            return None
        return await self.refresh(slug, head, reason="default branch moved")

    async def refresh(
        self, slug: str, sha: str | None = None, *, reason: str = "requested",
        force: bool = False,
    ) -> Refreshed | None:
        """Rebuild the atlas and charter at `sha` (default: the branch head)."""
        repo = self._store.repo(slug)
        if repo is None:
            return None
        if sha is None:
            sha = await asyncio.to_thread(remote_head, self._source(slug), repo.default_branch)
            if sha is None:
                sha = repo.default_branch  # let git resolve it at checkout
        if not force and sha == repo.charter_sha:
            return None

        lock = self._locks.setdefault(slug, asyncio.Lock())
        async with lock, self._gate:
            self._bus.emit("charter.building", repo=slug, sha=sha, reason=reason)
            try:
                return await asyncio.to_thread(functools.partial(self._rebuild, slug, sha))
            except Exception as error:  # noqa: BLE001 - a failed rebuild must not stop the service
                log.exception("charter.rebuild_failed", repo=slug, sha=sha)
                self._bus.emit("charter.failed", repo=slug, sha=sha, error=str(error)[:300])
                return None

    def _rebuild(self, slug: str, sha: str) -> Refreshed:
        entry = self._config.repo(slug)
        tree = worktree_for(slug, sha, url=entry.clone_url or None)
        toolchain = for_repo(tree, config=self._config, slug=slug)

        captured: dict[str, Any] = {}

        def persist(symbols: dict[str, Symbol], edges: dict[str, set[str]]) -> None:
            self._store.put_symbols(slug, list(symbols.values()))
            self._store.put_call_edges(slug, edges)
            captured["symbols"] = symbols

        atlas = build_atlas(
            tree, slug=slug, sha=sha, toolchain=toolchain,
            package_roots=tuple(entry.package_roots), on_structure=persist,
        )
        self._store.put_atlas(slug, sha, atlas)

        # The public API comes from the index the atlas already built, rather than
        # parsing the whole repository a second time.
        symbols: dict[str, Symbol] = captured.get("symbols", {})
        public = [
            fqn for fqn in symbols
            if not any(part.startswith("_") for part in fqn.split(".")[1:])
        ]
        fresh = extract_charter(
            tree, slug=slug, sha=sha, toolchain=toolchain,
            package_roots=tuple(entry.package_roots),
            public_api=public if symbols else None,
        )

        previous = self._store.charter(slug)
        stored = self._store.put_charter(fresh)
        # Declared norms come from the same files, so they move with the charter.
        self._store.replace_norms(slug, "declared", declared_norms(tree, toolchain))

        if previous is None:
            self._bus.emit(
                "charter.baseline", repo=slug, sha=sha, number=stored.number,
                claims=len(stored.claims),
            )
            log.info("charter.baseline", repo=slug, sha=sha[:12], claims=len(stored.claims))
            return Refreshed(charter=stored, drift=None, atlas_modules=len(atlas["modules"]))
        if previous.sha == stored.sha:
            # Rebuilt at a commit already remembered: the atlas is refreshed, the
            # memory is unchanged, and there is nothing to announce.
            self._bus.emit("charter.current", repo=slug, sha=sha, number=stored.number)
            return Refreshed(charter=stored, drift=None, atlas_modules=len(atlas["modules"]))

        drift = compare(previous, stored, self._config.charter)
        self._store.put_charter_change(drift)
        self._bus.emit(
            "charter.changed", repo=slug, from_sha=previous.sha, to_sha=stored.sha,
            level=drift.level, signals=len(drift.signals), number=stored.number,
        )
        log.info("charter.changed", repo=slug, level=drift.level, signals=len(drift.signals))

        if drift.level in ("notable", "major"):
            top = headline(drift)
            self._notifier.raise_(
                repo=slug,
                kind="repo.update",
                level=drift.level,
                title=(
                    f"{'Major' if drift.level == 'major' else 'Notable'} update to "
                    f"{slug}: {top.detail}"
                ),
                body=(
                    f"The default branch moved {previous.sha[:10]} → {stored.sha[:10]}. "
                    f"The system's memory of this repository was rebuilt as charter "
                    f"#{stored.number}; {len(drift.signals)} "
                    f"{'change' if len(drift.signals) == 1 else 'changes'} to what it is."
                ),
                sha=stored.sha,
                evidence=evidence_lines(drift),
            )
        return Refreshed(charter=stored, drift=drift, atlas_modules=len(atlas["modules"]))
