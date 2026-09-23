"""A local HTTP server that answers the handful of GitHub REST routes we call.

Not a mock of the client: the real `vcs.github.GitHub` makes real HTTP requests
through `httpx`, with real pagination and real status codes, to a server that
serves recorded payloads in GitHub's own response shape. What it cannot prove —
that api.github.com still answers this way — is covered by the live tests that
run against a real repository when the network allows.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class RecordedGitHub:
    """Routes map a path (no query) to a JSON payload; lists are paginated."""

    routes: dict[str, Any] = field(default_factory=dict)
    statuses: dict[str, tuple[int, str]] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    base_url: str = ""

    def pages_served(self, path: str) -> int:
        return sum(1 for request in self.requests if urlparse(request).path == path)


@contextmanager
def serve(recorded: RecordedGitHub) -> Iterator[RecordedGitHub]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            recorded.requests.append(self.path)
            parsed = urlparse(self.path)
            if parsed.path in recorded.statuses:
                status, text = recorded.statuses[parsed.path]
                self._send(status, {"message": text})
                return
            if parsed.path not in recorded.routes:
                self._send(404, {"message": "Not Found"})
                return
            payload = recorded.routes[parsed.path]
            if isinstance(payload, list):
                query = parse_qs(parsed.query)
                page = int(query.get("page", ["1"])[0])
                per_page = int(query.get("per_page", ["30"])[0])
                payload = payload[(page - 1) * per_page: page * per_page]
            self._send(200, payload)

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-RateLimit-Remaining", "4999")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    recorded.base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield recorded
    finally:
        server.shutdown()
        server.server_close()


def pull(number: int, *, merged: bool = True, updated: str = "2026-01-01T00:00:00Z",
         title: str = "") -> dict[str, Any]:
    return {
        "number": number, "title": title or f"change {number}", "body": "",
        "merged_at": updated if merged else None, "updated_at": updated,
        "html_url": f"https://github.com/acme/lib/pull/{number}",
        "user": {"login": "author"}, "state": "closed",
    }


def comment(comment_id: int, body: str, *, user: str, at: str, path: str = "lib/core.py"
            ) -> dict[str, Any]:
    return {
        "id": comment_id, "body": body, "user": {"login": user}, "created_at": at,
        "path": path, "diff_hunk": "@@ -1 +1 @@",
        "html_url": f"https://github.com/acme/lib/pull/0#discussion_r{comment_id}",
    }


def commit(at: str) -> dict[str, Any]:
    return {"commit": {"committer": {"date": at}, "author": {"date": at}}}
