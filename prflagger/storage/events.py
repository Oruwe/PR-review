"""The append-only event log, and the in-process fan-out that makes it live.

Everything a run does is an event: a state transition, a log line, a resource
sample, a test result, an observation. Each is written to `events` and then
published to whoever is subscribed.

The reason for both halves is the log console. A client that opens the page
mid-run must see the same transcript as one that was watching from the start.
So a subscriber starts buffering live events *first*, replays the table from its
cursor, and then drains the buffer discarding anything already replayed. There
is no window in which an event can be missed or shown twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog

from prflagger.storage.db import Database, connect, json_col

__all__ = [
    "Event",
    "EventBus",
    "Subscription",
    "bus",
]

log = structlog.get_logger(__name__)

#: A subscriber this far behind is dropped rather than allowed to grow without
#: bound. A browser tab that cannot keep up reconnects and replays; the server
#: staying alive matters more than that tab's continuity.
_QUEUE_LIMIT = 4096


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    type: str
    run_id: str | None
    repo: str | None
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "run_id": self.run_id,
            "repo": self.repo,
            **self.payload,
        }


def _row_to_event(row: Any) -> Event:
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return Event(
        seq=int(row["seq"]),
        ts=float(row["ts"]),
        type=str(row["type"]),
        run_id=row["run_id"],
        repo=row["repo"],
        payload=payload if isinstance(payload, dict) else {},
    )


class Subscription:
    """A live feed. Use `EventBus.subscribe` as a context manager to get one."""

    def __init__(self, bus_: EventBus, run_id: str | None, repo: str | None) -> None:
        self._bus = bus_
        self.run_id = run_id
        self.repo = repo
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        self.dropped = 0

    def wants(self, event: Event) -> bool:
        if self.run_id is not None and event.run_id != self.run_id:
            return False
        return not (self.repo is not None and event.repo != self.repo)

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()

    def close(self) -> None:
        self._bus.unsubscribe(self)


class EventBus:
    """Writes events durably, then hands them to live subscribers."""

    def __init__(self, database: Database | None = None) -> None:
        self._db = database or connect()
        self._subscribers: list[Subscription] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- wiring ---------------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Remember which loop subscribers live on.

        Probes and the sandbox runner are synchronous and may emit from a worker
        thread. Without this the fan-out would touch an `asyncio.Queue` from the
        wrong thread, which fails intermittently rather than loudly.
        """
        self._loop = loop or asyncio.get_running_loop()

    def subscribe(self, *, run_id: str | None = None, repo: str | None = None) -> Subscription:
        subscription = Subscription(self, run_id, repo)
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(subscription)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- writing --------------------------------------------------------------

    def emit(
        self,
        type_: str,
        *,
        run_id: str | None = None,
        repo: str | None = None,
        **payload: Any,
    ) -> int:
        """Append one event. Returns its sequence number.

        Synchronous on purpose: the callers that matter most (the sandbox line
        drain, the probes) are synchronous, and making them await would mean
        either threading an event loop through them or losing ordering.
        """
        now = time.time()
        cursor = self._db.execute(
            "INSERT INTO events (ts, run_id, repo, type, payload) VALUES (?, ?, ?, ?, ?)",
            (now, run_id, repo, type_, json_col(payload)),
        )
        seq = int(cursor.lastrowid or 0)
        event = Event(seq=seq, ts=now, type=type_, run_id=run_id, repo=repo, payload=payload)
        self._fan_out(event)
        return seq

    def _fan_out(self, event: Event) -> None:
        if not self._subscribers:
            return
        targets = [s for s in self._subscribers if s.wants(event)]
        if not targets:
            return
        loop = self._loop
        if loop is None or _on_loop(loop):
            for subscription in targets:
                subscription.offer(event)
            return
        # Emitted from a worker thread: hop to the loop that owns the queues.
        for subscription in targets:
            loop.call_soon_threadsafe(subscription.offer, event)

    # -- reading --------------------------------------------------------------

    def since(
        self,
        cursor: int = 0,
        *,
        run_id: str | None = None,
        repo: str | None = None,
        limit: int = 20_000,
    ) -> list[Event]:
        """Events after `cursor`, oldest first. This is the replay half."""
        sql = "SELECT * FROM events WHERE seq > ?"
        params: list[Any] = [cursor]
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if repo is not None:
            sql += " AND repo = ?"
            params.append(repo)
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        return [_row_to_event(row) for row in self._db.query(sql, params)]

    def head(self) -> int:
        """The highest sequence number written so far."""
        return int(self._db.scalar("SELECT MAX(seq) FROM events", default=0) or 0)

    # -- housekeeping ---------------------------------------------------------

    def prune(self, older_than_days: int = 7) -> int:
        """Drop events past the retention window. Returns how many went.

        Events for runs that are still in flight are kept regardless of age, so
        a long run never loses the start of its own transcript.
        """
        cutoff = time.time() - older_than_days * 86400
        cursor = self._db.execute(
            """
            DELETE FROM events
             WHERE ts < ?
               AND (run_id IS NULL
                    OR run_id IN (SELECT id FROM runs
                                   WHERE state IN ('done', 'failed', 'cancelled')))
            """,
            (cutoff,),
        )
        removed = int(cursor.rowcount or 0)
        if removed:
            log.info("events.pruned", removed=removed, older_than_days=older_than_days)
        return removed


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


_BUS: EventBus | None = None


def bus(database: Database | None = None) -> EventBus:
    """The process-wide event bus."""
    global _BUS
    if database is not None:
        return EventBus(database)
    if _BUS is None:
        _BUS = EventBus()
    return _BUS
