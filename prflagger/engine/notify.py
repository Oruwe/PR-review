"""Telling a person, at a volume that matches what happened.

Findings on a pull request are routine and live in the report. A change to what
a repository *is* is not routine, so it is announced differently, in three tiers:

  * **minor** — recorded in the repository's change history. No notification.
  * **notable** — a notification in the repository's change feed. It does not
    interrupt anyone.
  * **major** — a notification that stays on every page, visibly marked, until
    someone acknowledges it; and, when a webhook is configured, it is also
    pushed out.

Notification ids are derived from (repository, kind, commit), so the same
update seen twice — a restart, a second poll — never announces twice.

The webhook address comes from the environment, never from this repository:
a webhook URL is a credential.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
from typing import Any

import httpx
import structlog

from prflagger.core.config import Config
from prflagger.core.models import DRIFT_LEVELS, Notification
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store

__all__ = ["Notifier", "WEBHOOK_ENV", "notification_id"]

log = structlog.get_logger(__name__)

WEBHOOK_ENV = "PRFLAGGER_NOTIFY_WEBHOOK"

#: Only these tiers become notifications at all; minor drift is history, not news.
_NOTIFIED = ("notable", "major")


def notification_id(repo: str, kind: str, sha: str) -> str:
    digest = hashlib.sha256(f"{repo}\0{kind}\0{sha}".encode()).hexdigest()
    return f"n{digest[:20]}"


class Notifier:
    """Records notifications and delivers the loud ones."""

    def __init__(
        self,
        config: Config,
        store: Store,
        bus: EventBus,
        *,
        webhook_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        self._webhook = webhook_url if webhook_url is not None else os.environ.get(
            WEBHOOK_ENV, ""
        ).strip()
        self._client = client

    @property
    def webhook_configured(self) -> bool:
        return bool(self._webhook)

    def raise_(
        self,
        *,
        repo: str,
        kind: str,
        level: str,
        title: str,
        body: str,
        sha: str,
        evidence: Sequence[str] = (),
    ) -> Notification | None:
        """Record and announce. None when the level is too low, or already announced."""
        if level not in _NOTIFIED:
            return None
        notification = Notification(
            id=notification_id(repo, kind, sha), repo=repo, kind=kind, level=level,
            title=title, body=body, sha=sha, created_at=time.time(),
            evidence=tuple(evidence),
        )
        if not self._store.put_notification(notification):
            return None  # the same update, seen again

        self._bus.emit(
            "notification.created", repo=repo, notification_id=notification.id,
            kind=kind, level=level, title=title, sha=sha,
        )
        log.info("notification.created", repo=repo, kind=kind, level=level, sha=sha[:12])

        floor = self._config.charter.webhook_min_level
        if self._webhook and DRIFT_LEVELS.index(level) >= DRIFT_LEVELS.index(floor):
            self._store.record_delivery(notification.id, self._deliver(notification))
        return notification

    def _deliver(self, notification: Notification) -> dict[str, Any]:
        """POST to the webhook. Never raises: a failed delivery is recorded, not thrown.

        The body carries a top-level `text`, which is what Slack, Discord and most
        chat incoming-webhooks render, plus the structured record for anything
        that wants to parse it.
        """
        marker = "MAJOR" if notification.level == "major" else notification.level.upper()
        text = f"[{marker}] {notification.repo}: {notification.title}"
        if notification.body:
            text += f"\n{notification.body}"
        for line in notification.evidence[:6]:
            text += f"\n• {line}"
        payload = {
            "text": text,
            "prflagger": {
                "id": notification.id, "repo": notification.repo,
                "kind": notification.kind, "level": notification.level,
                "title": notification.title, "sha": notification.sha,
                "evidence": list(notification.evidence),
            },
        }
        started = time.monotonic()
        try:
            client = self._client or httpx.Client(timeout=10.0)
            try:
                response = client.post(self._webhook, json=payload)
            finally:
                if self._client is None:
                    client.close()
            outcome = {
                "channel": "webhook", "status": response.status_code,
                "ok": 200 <= response.status_code < 300,
                "ms": int((time.monotonic() - started) * 1000),
            }
        except httpx.HTTPError as error:
            outcome = {"channel": "webhook", "ok": False, "error": str(error)[:200]}
        if not outcome.get("ok"):
            log.warning("notification.delivery_failed", id=notification.id, **outcome)
        return outcome
