"""Signing in: who may read, who may change anything, and who may do neither.

Real HTTP through the real application and a real service; the only thing a
test chooses is which token it holds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from prflagger.api.app import create_app
from prflagger.api.auth import COOKIE, SESSION_S, Tokens
from prflagger.api.service import Service
from prflagger.core.config import Config, RepoConfig
from prflagger.core.models import Repo

ADMIN = "admin-token-for-tests-0123456789abcdef"
VIEWER = "viewer-token-for-tests-fedcba9876543210"


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Service:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    config = Config(repos=(RepoConfig(slug="demo/lib", clone_url=str(tmp_path / "src")),))
    built = Service.build(config, db_path=tmp_path / "auth.db")
    built.store.put_repo(Repo(slug="demo/lib", added_at=time.time()))
    return built


def _client(service: Service, tokens: Tokens) -> TestClient:
    return TestClient(create_app(service, config=service.config, watch=False, tokens=tokens))


@pytest.fixture
def client(service: Service) -> TestClient:
    return _client(service, Tokens(admin=ADMIN, viewer=VIEWER))


def _sign_in(client: TestClient, token: str) -> None:
    response = client.post("/login", data={"token": token, "next": "/"},
                           follow_redirects=False)
    assert response.status_code == 303, response.text


# ----------------------------------------------------------------------------------
# Nothing without signing in, except what must be reachable
# ----------------------------------------------------------------------------------


def test_a_stranger_is_sent_to_sign_in_and_the_api_refuses_them(client: TestClient) -> None:
    page = client.get("/repo/demo/lib?tab=x", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=%2Frepo%2Fdemo%2Flib%3Ftab%3Dx"
    assert client.get("/api/repos").status_code == 401
    assert client.post("/api/repos", json={"slug": "a/b"}).status_code == 401
    # What a load balancer and the sign-in page itself need stays open.
    assert client.get("/api/health").json()["auth"] == "tokens"
    assert client.get("/static/css/app.css").status_code == 200
    assert "Sign in" in client.get("/login").text


def test_the_sign_in_page_leaks_nothing(client: TestClient, service: Service) -> None:
    service.notifier.raise_(repo="demo/lib", kind="repo.update", level="major",
                            title="Major update to demo/lib: secret plans", body="b", sha="s")
    assert "secret plans" not in client.get("/login").text


def test_a_wrong_token_is_refused_and_the_right_one_signs_in(client: TestClient) -> None:
    wrong = client.post("/login", data={"token": "guess", "next": "/"}, follow_redirects=False)
    assert wrong.status_code == 401 and "not valid" in wrong.text
    assert COOKIE not in client.cookies

    _sign_in(client, ADMIN)
    cookie = client.cookies[COOKIE]
    assert cookie.startswith("admin|")
    assert client.get("/api/repos").status_code == 200
    assert client.get("/").status_code == 200


def test_the_session_cookie_cannot_be_forged_or_outlive_itself() -> None:
    tokens = Tokens(admin=ADMIN, viewer=VIEWER)
    now = 1_800_000_000.0
    good = tokens.issue("viewer", now=now)
    assert tokens.verify(good, now=now) == "viewer"

    role, expires, signature = good.split("|")
    assert tokens.verify(f"admin|{expires}|{signature}", now=now) is None, "role swapped"
    assert tokens.verify(f"{role}|{int(expires) + 999}|{signature}", now=now) is None
    assert tokens.verify(good, now=now + SESSION_S + 1) is None, "expired"
    assert Tokens(admin="rotated").verify(good, now=now) is None, "rotation signs out"
    assert Tokens(admin=ADMIN).verify(good, now=now) is None, "viewer token withdrawn"


def test_a_viewer_reads_everything_and_changes_nothing(client: TestClient) -> None:
    _sign_in(client, VIEWER)
    assert client.get("/api/repos").status_code == 200
    page = client.get("/repo/demo/lib/prs").text
    assert "viewer" in page and "Sign out" in page
    assert 'id="poll"' not in page, "the controls a viewer cannot use are not shown"
    refused = client.post("/api/repos/demo/lib/poll", json={})
    assert refused.status_code == 403 and "read-only" in refused.json()["detail"]


def test_scripts_use_a_bearer_token(client: TestClient) -> None:
    assert client.get("/api/repos", headers={"Authorization": f"Bearer {ADMIN}"}
                      ).status_code == 200
    assert client.post("/api/repos/demo/lib/poll", json={},
                       headers={"Authorization": f"Bearer {VIEWER}"}).status_code == 403
    assert client.get("/api/repos", headers={"Authorization": "Bearer nope"}
                      ).status_code == 401


def test_a_cross_origin_post_is_refused_even_when_signed_in(client: TestClient) -> None:
    _sign_in(client, ADMIN)
    evil = client.post("/api/notifications/x/ack", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    same = client.post("/api/notifications/x/ack", headers={"Origin": "http://testserver"})
    assert same.status_code == 200


def test_the_live_socket_needs_a_session_too(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as refused, \
            client.websocket_connect("/ws/repo/demo/lib"):
        pass
    assert refused.value.code == 4401
    _sign_in(client, VIEWER)
    with client.websocket_connect("/ws/repo/demo/lib") as socket:
        assert socket is not None


def test_a_socket_opened_from_another_site_is_refused(client: TestClient) -> None:
    _sign_in(client, ADMIN)
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
        "/ws/repo/demo/lib", headers={"Origin": "https://evil.example"}
    ):
        pass
    assert refused.value.code == 4401


def test_a_guessable_token_is_refused_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRFLAGGER_ADMIN_TOKEN", "hunter2")
    with pytest.raises(ValueError, match="at least 20"):
        Tokens.from_env()


def test_after_sign_in_only_this_site_is_a_destination(client: TestClient) -> None:
    for target in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        response = client.post("/login", data={"token": ADMIN, "next": target},
                               follow_redirects=False)
        assert response.headers["location"] == "/", target


def test_with_no_token_the_service_is_open_for_local_use(service: Service) -> None:
    open_client = _client(service, Tokens())
    assert open_client.get("/api/repos").status_code == 200
    assert open_client.get("/api/health").json()["auth"] == "open (loopback only)"
    assert "Sign out" not in open_client.get("/").text


# ----------------------------------------------------------------------------------
# The service refuses to be exposed without one
# ----------------------------------------------------------------------------------


def _serve(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **{k: v for k, v in os.environ.items()
           if k not in ("PRFLAGGER_ADMIN_TOKEN", "PRFLAGGER_VIEWER_TOKEN")},
        "PRFLAGGER_CACHE_DIR": str(tmp_path / "cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        **env,
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "prflagger.cli", "serve", "--no-watch", *args],
        capture_output=True, text=True, env=environment, cwd=tmp_path, timeout=60,
        check=False,
    )


def test_serve_will_not_listen_publicly_without_a_token(tmp_path: Path) -> None:
    refused = _serve(tmp_path, "--host", "0.0.0.0", "--port", "8139")  # noqa: S104
    assert refused.returncode == 2
    assert "PRFLAGGER_ADMIN_TOKEN" in refused.stderr

    lonely = _serve(tmp_path, "--port", "8139", PRFLAGGER_VIEWER_TOKEN="v" * 32)
    assert lonely.returncode == 2 and "viewer token alone" in lonely.stderr
