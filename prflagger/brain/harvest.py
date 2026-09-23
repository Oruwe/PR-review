"""Fetch merged pull requests with their review comments and commits.

v1 shelled out to the `gh` CLI, which is not installed everywhere this runs —
including where it was developed, so the Brain was dead on arrival there. This
goes through `vcs.github.GitHub` over REST, so the only requirement is network
access and, for anything beyond a handful of pull requests, a token in the
environment.

Two callers, two shapes of use:

* `harvest()` — the v1 one-shot path behind `prflagger brain build`. It writes
  the raw records to `.cache/brain/<slug>/prs.json` and never re-fetches what is
  already on disk.
* `fetch_reviews()` — the service's incremental path. It walks closed pull
  requests newest-first and stops at the last one it already saw, so a daily
  refresh of a busy repository costs a few requests rather than a few hundred.

Raw responses are kept in GitHub's own shape; filtering happens in `enforce.py`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

from prflagger.sandbox.runner import cache_root
from prflagger.vcs.github import GitHub, GitHubError

__all__ = ["GhUnavailable", "HarvestUnavailable", "fetch_reviews", "harvest", "prs_path"]

log = structlog.get_logger(__name__)


class HarvestUnavailable(RuntimeError):
    """GitHub could not be reached, or refused the request."""


#: The v1 name. Kept so existing callers and scripts keep working; there is no
#: longer anything `gh`-specific about it.
GhUnavailable = HarvestUnavailable


def prs_path(repo_slug: str) -> Path:
    return cache_root() / "brain" / repo_slug.replace("/", "__") / "prs.json"


def fetch_reviews(
    github: GitHub,
    slug: str,
    *,
    limit: int = 150,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Merged pull requests, newest first, each with its review comments and commits.

    `limit` bounds how many *closed* pull requests are examined; `since` is the
    `updated_at` of the newest one a previous call already recorded, and the walk
    stops there. Each merged pull request costs two further requests (comments,
    commits), which is why both bounds exist.
    """
    try:
        closed = github.closed_pulls(slug, limit=limit, since=since)
    except GitHubError as error:
        raise HarvestUnavailable(str(error)) from error

    records: list[dict[str, Any]] = []
    for pull in closed:
        if not pull.get("merged_at"):
            continue  # an unmerged pull request enforced nothing; do not pay for it
        number = int(pull["number"])
        try:
            comments = github.review_comments(slug, number)
            commits = github.pull_commits(slug, number)
        except GitHubError as error:
            raise HarvestUnavailable(f"{slug}#{number}: {error}") from error
        records.append({
            "number": number,
            "title": pull.get("title", ""),
            "body": pull.get("body") or "",
            "merged_at": pull.get("merged_at"),
            "updated_at": pull.get("updated_at"),
            "html_url": pull.get("html_url", ""),
            "user": (pull.get("user") or {}).get("login", ""),
            "review_comments": comments,
            "commits": commits,
        })
    return records


def harvest(repo_slug: str, *, limit: int = 150, github: GitHub | None = None) -> Path:
    """Fetch merged PRs with review comments and commits into `prs.json`.

    Never re-fetches what is cached: rate limits bite, and a demo must never
    depend on the network.
    """
    path = prs_path(repo_slug)
    if path.is_file():
        log.info("harvest.cached", repo=repo_slug, path=str(path))
        return path

    started = time.monotonic()
    client = github or GitHub()
    try:
        records = fetch_reviews(client, repo_slug, limit=limit)
    finally:
        if github is None:
            client.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    log.info(
        "harvested",
        repo=repo_slug,
        merged=len(records),
        requests=client.requests_made,
        wall_s=round(time.monotonic() - started, 1),
    )
    return path
