"""The enforcement filter — the single most important step in the Brain.

A review comment only counts if the author actually changed the code after it and the
pull request then merged. That chain is what separates a standard that was *enforced*
from an opinion that was ignored.

Without it you are mining every stray thought anyone typed into a review box, including
the ones the author correctly disregarded.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

__all__ = ["enforced_comments", "retention_rate"]

log = structlog.get_logger(__name__)


def enforced_comments(prs_json: Path) -> list[dict[str, Any]]:
    """Keep a review comment only if (a) a commit on that PR has a timestamp AFTER the
    comment's, and (b) the PR merged. Returns dicts with:
    pr_number, reviewer_login, body, diff_hunk, created_at."""
    try:
        records = json.loads(prs_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(records, list):
        return []

    kept: list[dict[str, Any]] = []
    seen = 0
    for pull in records:
        if not isinstance(pull, dict):
            continue
        comments = [c for c in pull.get("review_comments", []) if isinstance(c, dict)]
        seen += len(comments)

        if not pull.get("merged_at"):
            continue  # an unmerged PR enforced nothing

        commit_times = sorted(
            filter(None, (_commit_time(commit) for commit in pull.get("commits", [])))
        )
        if not commit_times:
            continue
        latest = commit_times[-1]

        for comment in comments:
            created = _parse(comment.get("created_at"))
            if created is None or created >= latest:
                continue  # nothing landed after it, so nothing was enforced
            kept.append(
                {
                    "pr_number": int(pull.get("number", 0)),
                    "reviewer_login": str((comment.get("user") or {}).get("login", "")),
                    "body": str(comment.get("body", "")),
                    "diff_hunk": str(comment.get("diff_hunk", "")),
                    "created_at": str(comment.get("created_at", "")),
                }
            )

    rate = retention_rate(seen, len(kept))
    log.info("enforced", seen=seen, kept=len(kept), retention_rate=round(rate, 4))
    return kept


def retention_rate(seen: int, kept: int) -> float:
    return 0.0 if seen == 0 else kept / seen


def _commit_time(commit: Any) -> datetime | None:
    if not isinstance(commit, dict):
        return None
    detail = commit.get("commit") or {}
    for slot in ("committer", "author"):
        stamp = (detail.get(slot) or {}).get("date")
        parsed = _parse(stamp)
        if parsed is not None:
            return parsed
    return None


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
