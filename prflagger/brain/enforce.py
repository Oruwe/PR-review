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

__all__ = ["enforced_comments", "judge_comments", "retention_rate"]

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

    judged = judge_comments(records)
    kept = [
        {key: comment[key] for key in _DOCUMENTED_FIELDS}
        for comment in judged
        if comment["enforced"]
    ]
    rate = retention_rate(len(judged), len(kept))
    log.info("enforced", seen=len(judged), kept=len(kept), retention_rate=round(rate, 4))
    return kept


_DOCUMENTED_FIELDS = ("pr_number", "reviewer_login", "body", "diff_hunk", "created_at")


def judge_comments(records: list[Any]) -> list[dict[str, Any]]:
    """Every review comment in `records`, each marked `enforced` or not.

    The service keeps the ones that were not enforced too, so the retention rate
    it reports is measured rather than remembered. Beyond the five documented
    fields each carries `comment_id`, `path` and `html_url`, which is what lets a
    norm cite the exact comment it came from.
    """
    out: list[dict[str, Any]] = []
    for pull in records:
        if not isinstance(pull, dict):
            continue
        merged = bool(pull.get("merged_at"))
        commit_times = sorted(
            filter(None, (_commit_time(commit) for commit in pull.get("commits", [])))
        )
        latest = commit_times[-1] if commit_times else None
        for comment in pull.get("review_comments", []):
            if not isinstance(comment, dict):
                continue
            created = _parse(comment.get("created_at"))
            # An unmerged PR enforced nothing, and a comment with nothing landing
            # after it was, as far as the history can show, not acted on.
            enforced = (
                merged and latest is not None and created is not None and created < latest
            )
            out.append({
                "pr_number": int(pull.get("number", 0)),
                "reviewer_login": str((comment.get("user") or {}).get("login", "")),
                "body": str(comment.get("body", "")),
                "diff_hunk": str(comment.get("diff_hunk", "")),
                "created_at": str(comment.get("created_at", "")),
                "comment_id": int(comment.get("id") or 0),
                "path": str(comment.get("path") or ""),
                "html_url": str(comment.get("html_url") or ""),
                "enforced": enforced,
            })
    return out


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
