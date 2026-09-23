"""The WebSocket: replay from a cursor, then go live, with no gap and no repeat.

The order matters and is the whole trick. Subscribing *before* reading the
backlog means events arriving during the read are buffered rather than lost;
discarding buffered events at or below the last replayed sequence means none is
delivered twice. A tab opened at the end of a run therefore sees exactly the
transcript a tab opened at the start saw.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from fastapi import WebSocket, WebSocketDisconnect

from prflagger.storage.events import EventBus

__all__ = ["stream_events"]

log = structlog.get_logger(__name__)

#: Long enough that an idle connection stays open through a slow image build,
#: short enough that a dead peer is noticed.
_PING_SECONDS = 25.0

#: One replay chunk. A long run's transcript is sent in pieces so the socket
#: stays responsive rather than blocking on one enormous frame.
_CHUNK = 500


async def stream_events(
    websocket: WebSocket,
    bus: EventBus,
    *,
    run_id: str | None = None,
    repo: str | None = None,
    cursor: int = 0,
) -> None:
    """Serve one client until it disconnects."""
    await websocket.accept()
    subscription = bus.subscribe(run_id=run_id, repo=repo)
    delivered = cursor

    try:
        # Replay. The subscription is already buffering, so nothing written
        # during this loop is lost.
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

        await websocket.send_json({"type": "live", "cursor": delivered})

        pinger = asyncio.create_task(_ping(websocket))
        try:
            while True:
                event = await subscription.queue.get()
                if event.seq <= delivered:
                    continue  # already replayed; do not show it twice
                delivered = event.seq
                await websocket.send_json({"type": "event", "event": event.as_dict()})
                if subscription.dropped:
                    # Tell the client rather than silently skipping: it can
                    # reconnect with its cursor and get the missing stretch.
                    await websocket.send_json(
                        {"type": "dropped", "count": subscription.dropped, "cursor": delivered}
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


async def _ping(websocket: WebSocket) -> None:
    while True:
        await asyncio.sleep(_PING_SECONDS)
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "ping"})
