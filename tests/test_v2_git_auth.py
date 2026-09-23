"""Private repositories: the token reaches the server, and nowhere else.

A real bare repository is served over git's HTTP protocol by a local server
that records each request's headers; real `git` clones it. What is checked is
what went over the wire and what was left on disk.
"""

from __future__ import annotations

import base64
import functools
import http.server
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from prflagger.vcs.credentials import git_env
from prflagger.vcs.git import remote_head
from prflagger.vcs.worktrees import bare_clone
from tests.v2_fixtures import build_repo

TOKEN = "ghp_test0000000000000000000000000000000000"


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, list[dict[str, str]]]]:
    """A bare copy of the fixture repository, served over HTTP; yields (base, headers)."""
    source, _, _ = build_repo(tmp_path / "source")
    root = tmp_path / "www"
    bare = root / "acme" / "private.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True)  # noqa: S603, S607
    subprocess.run(["git", "--git-dir", str(bare), "update-server-info"], check=True)  # noqa: S603, S607
    seen: list[dict[str, str]] = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            seen.append({k.lower(): v for k, v in self.headers.items()})
            super().do_GET()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()


def _everything_under(path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in path.rglob("*") if p.is_file())


def test_the_token_goes_with_the_request_and_is_never_written_down(
    served: tuple[str, list[dict[str, str]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, seen = served
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    # This local server plays the configured GitHub host.
    monkeypatch.setenv("PRFLAGGER_GITHUB_API", f"{base}/api/v3")

    clone = bare_clone("acme/private", url=f"{base}/acme/private.git")
    assert (clone / "HEAD").is_file(), "the clone succeeded"

    expected = "basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert seen and all(h.get("authorization") == expected for h in seen)

    on_disk = _everything_under(clone)
    assert TOKEN.encode() not in on_disk
    assert base64.b64encode(f"x-access-token:{TOKEN}".encode()) not in on_disk
    config = (clone / "config").read_text()
    assert f"{base}/acme/private.git" in config and "@" not in config.split("url =")[1]

    before = len(seen)
    assert remote_head(f"{base}/acme/private.git", "main") is not None
    assert all(h.get("authorization") == expected for h in seen[before:])


def test_the_token_never_goes_to_any_other_host(
    served: tuple[str, list[dict[str, str]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, seen = served
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    # github.com is the configured host; this server is not it.
    monkeypatch.delenv("PRFLAGGER_GITHUB_API", raising=False)

    bare_clone("acme/other", url=f"{base}/acme/private.git")
    assert seen and all("authorization" not in h for h in seen)


def test_without_a_token_git_runs_as_it_always_did(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "PRFLAGGER_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert git_env("https://github.com/acme/private") is None


@pytest.mark.parametrize(("url", "sent"), [
    ("https://github.com/acme/private", True),
    ("https://github.com.evil.example/acme/private", False),
    ("http://github.com/acme/private", False),
    ("https://gitlab.com/acme/private", False),
    ("/srv/git/private.git", False),
    ("git@github.com:acme/private.git", False),
])
def test_only_the_configured_host_over_https(
    url: str, sent: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("PRFLAGGER_GITHUB_API", raising=False)
    before = int(__import__("os").environ.get("GIT_CONFIG_COUNT", "0") or 0)
    env = git_env(url)
    assert (env is not None) is sent
    if env is not None:
        # Appended after whatever git configuration the environment already had.
        assert env["GIT_CONFIG_COUNT"] == str(before + 1)
        assert env[f"GIT_CONFIG_KEY_{before}"] == "http.https://github.com/.extraheader"


def test_a_refused_token_falls_back_to_reading_anonymously(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public repository must still clone when the token is expired or revoked."""
    refused: list[str] = []

    class Refusing(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.headers.get("Authorization"):
                refused.append(self.path)
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
                self.end_headers()
                return
            self.send_response(404)  # anonymous reaches the server: that is the point
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("PRFLAGGER_GITHUB_API", f"{base}/api/v3")
    try:
        from prflagger.vcs.credentials import run_git

        completed = run_git(["git", "ls-remote", f"{base}/acme/public.git"],
                            remote=f"{base}/acme/public.git", timeout_s=30)
    finally:
        server.shutdown()
        server.server_close()
    assert refused, "the token was offered first"
    assert "401" not in completed.stderr and "Username" not in completed.stderr, (
        "the anonymous retry's own result is what is reported"
    )
