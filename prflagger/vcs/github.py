"""GitHub over REST.

v1 shelled out to the `gh` CLI, which is not installed in every environment this
runs in — including the one it was developed in, where the entire Brain was
therefore dead on arrival. This talks to the API directly over `httpx`, so the
only requirement is a token in the environment, and public repositories work
without one.

Three things a polling service needs and a one-shot CLI does not: conditional
requests so an unchanged PR list costs no rate-limit quota, rate-limit awareness
so a burst backs off instead of being refused, and pagination that stops at a
caller-set bound rather than walking a decade of history.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from prflagger.core.errors import RepoUnavailable
from prflagger.core.models import PullRequest

__all__ = ["GitHub", "GitHubError", "token_from_env"]

log = structlog.get_logger(__name__)

_API = "https://api.github.com"
#: GitHub Enterprise Server serves the same REST API at https://<host>/api/v3.
_API_VAR = "PRFLAGGER_GITHUB_API"
_TOKEN_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "PRFLAGGER_GITHUB_TOKEN")


class GitHubError(RepoUnavailable):
    """The API refused, or could not be reached."""


def token_from_env() -> str:
    """The first token the environment offers. Never read from the repository."""
    for variable in _TOKEN_VARS:
        value = os.environ.get(variable, "").strip()
        if value:
            return value
    return ""


@dataclass
class _Cached:
    etag: str
    payload: Any


class GitHub:
    """A small, polite GitHub client.

    Not a general-purpose wrapper: it exposes exactly what the watcher and the
    Brain need, so the surface that can break when the API changes stays small.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token if token is not None else token_from_env()
        self._base = (base_url or os.environ.get(_API_VAR, "").strip() or _API).rstrip("/")
        self._cache: dict[str, _Cached] = {}
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)
        self.remaining: int | None = None
        self.resets_at: float | None = None
        self.requests_made = 0
        self.conditional_hits = 0

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def close(self) -> None:
        self._client.close()

    # -- transport ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "prflagger/2",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(
        self, path: str, params: dict[str, Any] | None = None, *, conditional: bool = True
    ) -> Any:
        url = path if path.startswith("http") else f"{self._base}{path}"
        cache_key = f"{url}?{sorted((params or {}).items())}"
        headers = self._headers()
        cached = self._cache.get(cache_key) if conditional else None
        if cached is not None:
            headers["If-None-Match"] = cached.etag

        self._wait_for_quota()
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as error:
            raise GitHubError(f"GET {path} failed: {error}") from error
        self.requests_made += 1
        self._record_limits(response)

        if response.status_code == 304 and cached is not None:
            self.conditional_hits += 1
            return cached.payload
        if response.status_code == 404:
            raise GitHubError(f"{path} not found (private repository without a token?)")
        if response.status_code == 403 and "rate limit" in response.text.lower():
            raise GitHubError(
                "GitHub rate limit exhausted"
                + ("" if self.token else "; set GITHUB_TOKEN to raise it from 60/hour to 5000")
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"GET {path} returned {response.status_code}: {response.text[:300]}"
            )

        payload = response.json()
        etag = response.headers.get("ETag")
        if etag and conditional:
            self._cache[cache_key] = _Cached(etag=etag, payload=payload)
        return payload

    def _record_limits(self, response: httpx.Response) -> None:
        raw_remaining = response.headers.get("X-RateLimit-Remaining")
        raw_reset = response.headers.get("X-RateLimit-Reset")
        if raw_remaining and raw_remaining.isdigit():
            self.remaining = int(raw_remaining)
        if raw_reset and raw_reset.isdigit():
            self.resets_at = float(raw_reset)

    def _wait_for_quota(self) -> None:
        """Pause rather than be refused when the window is nearly spent."""
        if self.remaining is None or self.remaining > 2 or self.resets_at is None:
            return
        delay = max(0.0, self.resets_at - time.time()) + 1.0
        if delay > 0:
            log.warning("github.rate_limited", sleeping_s=round(delay, 1))
            time.sleep(min(delay, 300.0))

    def paged(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 100,
        stop: Callable[[dict[str, Any]], bool] | None = None,
        conditional: bool = True,
    ) -> list[dict[str, Any]]:
        """Follow `page` until `limit` items, the API runs out, or `stop` says so.

        `stop` is checked per item, in order; the first item it accepts ends the
        walk and is not returned. That is what makes an incremental harvest cost
        only the pages that hold something new.
        """
        out: list[dict[str, Any]] = []
        page, per_page = 1, min(100, max(1, limit))
        while len(out) < limit:
            batch = self._get(
                path, {**(params or {}), "per_page": per_page, "page": page},
                conditional=conditional,
            )
            if not isinstance(batch, list) or not batch:
                break
            for item in batch:
                if stop is not None and isinstance(item, dict) and stop(item):
                    return out[:limit]
                out.append(item)
            if len(batch) < per_page:
                break
            page += 1
        return out[:limit]

    # -- what the service actually asks for -----------------------------------

    def repo(self, slug: str) -> dict[str, Any]:
        payload = self._get(f"/repos/{slug}")
        if not isinstance(payload, dict):
            raise GitHubError(f"unexpected response shape for {slug}")
        return payload

    def default_branch(self, slug: str) -> str:
        return str(self.repo(slug).get("default_branch") or "main")

    def open_pulls(self, slug: str, *, limit: int = 25) -> list[PullRequest]:
        """Open PRs, most recently updated first."""
        raw = self.paged(
            f"/repos/{slug}/pulls",
            {"state": "open", "sort": "updated", "direction": "desc"},
            limit=limit,
        )
        return [self._pull(slug, entry) for entry in raw]

    def pull(self, slug: str, number: int) -> PullRequest:
        payload = self._get(f"/repos/{slug}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError(f"unexpected response shape for {slug}#{number}")
        return self._pull(slug, payload)

    def closed_pulls(
        self, slug: str, *, limit: int = 150, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Closed PRs, most recently updated first, stopping at `since` (an
        `updated_at` already seen). Not conditional: a harvest walks history once,
        and caching every page's ETag would grow without bound in a long-lived
        process."""
        return self.paged(
            f"/repos/{slug}/pulls",
            {"state": "closed", "sort": "updated", "direction": "desc"},
            limit=limit,
            stop=(lambda item: str(item.get("updated_at") or "") <= since) if since else None,
            conditional=False,
        )

    # Review comments and commits of a merged pull request do not change, so
    # neither is worth an ETag entry that would live as long as the process.

    def review_comments(
        self, slug: str, number: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.paged(
            f"/repos/{slug}/pulls/{number}/comments", limit=limit, conditional=False
        )

    def pull_commits(
        self, slug: str, number: int, *, limit: int = 250
    ) -> list[dict[str, Any]]:
        return self.paged(
            f"/repos/{slug}/pulls/{number}/commits", limit=limit, conditional=False
        )

    def compare(self, slug: str, base: str, head: str) -> dict[str, Any]:
        payload = self._get(f"/repos/{slug}/compare/{base}...{head}", conditional=False)
        if not isinstance(payload, dict):
            raise GitHubError(f"unexpected compare response for {slug}")
        return payload

    @staticmethod
    def _pull(slug: str, entry: dict[str, Any]) -> PullRequest:
        base = entry.get("base") or {}
        head = entry.get("head") or {}
        user = entry.get("user") or {}
        merged = bool(entry.get("merged_at"))
        state = "merged" if merged else str(entry.get("state") or "open")
        return PullRequest(
            repo=slug,
            number=int(entry.get("number") or 0),
            title=str(entry.get("title") or ""),
            body=str(entry.get("body") or ""),
            author=str(user.get("login") or ""),
            base_sha=str(base.get("sha") or ""),
            head_sha=str(head.get("sha") or ""),
            state=state,
            updated_at=str(entry.get("updated_at") or ""),
            additions=int(entry.get("additions") or 0),
            deletions=int(entry.get("deletions") or 0),
            changed_files=int(entry.get("changed_files") or 0),
            draft=bool(entry.get("draft")),
        )
