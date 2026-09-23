"""The WebSocket: replay from a cursor, then go live, with no gap and no repeat.

Two sources are replayed, because the two kinds of record are stored
differently. State changes, job boundaries and resource samples are rows in
`events` and replay from a sequence cursor. Log lines are not rows — they would
be the one unbounded dimension in that table — so they replay from the per-job
NDJSON transcript the sandbox wrote, against a separate line cursor.

The ordering is what makes this correct. Subscribing *before* reading either
source means anything arriving during the read is buffered rather than lost;
discarding buffered records at or below what was already sent means nothing is
delivered twice. A tab opened at the end of a run sees exactly the transcript a
tab opened at the start saw.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from prflagger.core.config import cache_root
from prflagger.storage.events import EventBus

__all__ = ["stream_events"]

log = structlog.get_logger(__name__)

#: Long enough that an idle connection survives a slow image build, short enough
#: that a dead peer is noticed.
_PING_SECONDS = 25.0

#: One replay chunk. A long run's transcript goes in pieces so the socket stays
#: responsive rather than blocking on one enormous frame.
_CHUNK = 500

#: A ceiling on what one client can pull in a single replay. Past this the tail
#: is what matters; the whole file is still downloadable from /api/runs/{id}/log.
_MAX_REPLAY_LINES = 20_000


async def stream_events(
    websocket: WebSocket,
    bus: EventBus,
    *,
    run_id: str | None = None,
    repo: str | None = None,
    cursor: int = 0,
    lines: int = 0,
) -> None:
    """Serve one client until it disconnects.

    `cursor` is the highest `events.seq` the client already has; `lines` is how
    many transcript lines it already rendered. A reconnect passes both back, so
    it resumes rather than restarting or losing the middle.
    """
    await websocket.accept()
    subscription = bus.subscribe(run_id=run_id, repo=repo)
    delivered = cursor

    try:
        # Rows first: state transitions, job boundaries, samples.
        while True:
            batch = bus.since(delivered, run_id=run_id, repo=repo, limit=_CHUNK)
            if not batch:
                break
            await websocket.send_json(
                {"type": "batch", "events": [event.as_dict() for event in batch]}
            )
            delivered = batch[-1].seq
            if len(batch) < _CHUNK:
                break

        # Then the transcript from disk, skipping what the client already has.
        replayed_lines = 0
        if run_id:
            replayed_lines = await _replay_transcript(websocket, run_id, skip=lines)

        await websocket.send_json(
            {"type": "live", "cursor": delivered, "lines": lines + replayed_lines}
        )

        pinger = asyncio.create_task(_ping(websocket))
        try:
            while True:
                event = await subscription.queue.get()
                # Rows carry a sequence; published records (log lines) carry 0
                # and are never replayed from the table, so they always pass.
                if event.seq and event.seq <= delivered:
                    continue
                if event.seq:
                    delivered = event.seq
                await websocket.send_json({"type": "event", "event": event.as_dict()})
                if subscription.dropped:
                    # Say so rather than silently skipping: the client
                    # reconnects with its cursors and refills the gap.
                    await websocket.send_json(
                        {"type": "dropped", "count": subscription.dropped,
                         "cursor": delivered}
                    )
                    subscription.dropped = 0
        finally:
            pinger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pinger
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001 - one bad socket must not disturb the engine
        log.exception("ws.failed", run=run_id, repo=repo)
    finally:
        subscription.close()


async def _replay_transcript(websocket: WebSocket, run_id: str, *, skip: int) -> int:
    """Send the run's stored output, oldest job first. Returns how many lines went.

    Read off the event loop: a large transcript is a lot of disk, and blocking
    here would stall every other socket the service is serving.
    """
    directory = cache_root() / "runs" / run_id
    if not directory.is_dir():
        return 0

    seen = 0
    sent = 0
    batch: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.ndjson")):
        job_id = path.stem
        for record in await asyncio.to_thread(_read_lines, path):
            seen += 1
            if seen <= skip:
                continue
            if sent >= _MAX_REPLAY_LINES:
                batch.append({
                    "type": "log.line", "run_id": run_id, "job_id": job_id,
                    "stream": "meta", "offset_ms": 0,
                    "text": (
                        f"… replay limited to {_MAX_REPLAY_LINES:,} lines; "
                        f"download the full transcript for the rest"
                    ),
                })
                break
            record["type"] = "log.line"
            record["run_id"] = run_id
            record["job_id"] = job_id
            batch.append(record)
            sent += 1
            if len(batch) >= _CHUNK:
                await websocket.send_json({"type": "batch", "events": batch})
                batch = []
        if sent >= _MAX_REPLAY_LINES:
            break

    if batch:
        await websocket.send_json({"type": "batch", "events": batch})
    return sent


def _read_lines(path: Path) -> list[dict[str, object]]:
    """Parse one NDJSON transcript. A truncated tail is a short read, not a crash.

    A transcript is being appended to while it is read — the run may still be
    going — so the last line can be half-written.
    """
    out: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        return out
    return out


async def _ping(websocket: WebSocket) -> None:
    while True:
        await asyncio.sleep(_PING_SECONDS)
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "ping"})
