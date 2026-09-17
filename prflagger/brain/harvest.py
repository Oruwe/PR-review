"""Fetch merged pull requests and cache them.

The Brain is built ahead of time and read at request time. A PR check that waited on two
hundred API calls would be unusable in CI, which is why harvesting never appears in the
per-PR path.

Raw responses are stored unmodified; filtering happens in enforce.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import structlog

from prflagger.sandbox.runner import cache_root

__all__ = ["GhUnavailable", "harvest", "prs_path"]

log = structlog.get_logger(__name__)


class GhUnavailable(RuntimeError):
    """The `gh` CLI is missing or cannot reach the API."""


def prs_path(repo_slug: str) -> Path:
    return cache_root() / "brain" / repo_slug.replace("/", "__") / "prs.json"


def harvest(repo_slug: str, *, limit: int = 150) -> Path:
    """Fetch merged PRs via `gh` CLI: metadata, review comments, commits with timestamps.
    Write to .cache/brain/<slug>/prs.json. Never re-fetch what is cached."""
    path = prs_path(repo_slug)
    if path.is_file():
        # Never re-fetch what is cached: rate limits bite, and a demo must never
        # depend on the network.
        log.info("harvest.cached", repo=repo_slug, path=str(path))
        return path

    if shutil.which("gh") is None:
        raise GhUnavailable(
            "the gh CLI is not installed; harvest needs it to read merged pull requests"
        )

    started = time.monotonic()
    pulls = _paged(
        f"repos/{repo_slug}/pulls",
        {"state": "closed", "sort": "updated", "direction": "desc"},
        limit,
    )
    merged = [pull for pull in pulls if pull.get("merged_at")]

    records: list[dict[str, Any]] = []
    for pull in merged:
        number = int(pull["number"])
        records.append(
            {
                "number": number,
                "title": pull.get("title", ""),
                "body": pull.get("body") or "",
                "merged_at": pull.get("merged_at"),
                "user": (pull.get("user") or {}).get("login", ""),
                "review_comments": _paged(
                    f"repos/{repo_slug}/pulls/{number}/comments", {}, 100
                ),
                "commits": _paged(f"repos/{repo_slug}/pulls/{number}/commits", {}, 100),
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    log.info(
        "harvested",
        repo=repo_slug,
        merged=len(records),
        wall_s=round(time.monotonic() - started, 1),
    )
    return path


def _paged(endpoint: str, params: dict[str, str], limit: int) -> list[dict[str, Any]]:
    """`gh api --paginate`, which handles Link headers for us."""
    query = "".join(f"&{key}={value}" for key, value in params.items())
    argv = [
        "gh",
        "api",
        "--paginate",
        "--method",
        "GET",
        f"{endpoint}?per_page=100{query}",
    ]
    completed = _gh(argv)
    if completed.returncode != 0:
        raise GhUnavailable(f"gh api {endpoint} failed: {completed.stderr.strip()[:300]}")
    items: list[dict[str, Any]] = []
    # --paginate concatenates JSON arrays; decode them one after another.
    decoder = json.JSONDecoder()
    index = 0
    text = completed.stdout
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        payload, index = decoder.raw_decode(text, index)
        if isinstance(payload, list):
            items.extend(item for item in payload if isinstance(item, dict))
        if len(items) >= limit:
            break
    return items[:limit]


def _gh(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """The single point where this process shells out to gh."""
    return subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, check=False, timeout=300
    )
