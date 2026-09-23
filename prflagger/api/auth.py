"""Signing in with shared tokens.

Two tokens, both read from the environment and never from a file in the
repository:

* ``PRFLAGGER_ADMIN_TOKEN`` — may do everything: add repositories, start and
  cancel runs, rebuild memory, acknowledge alerts.
* ``PRFLAGGER_VIEWER_TOKEN`` (optional) — may read everything and change nothing.

With no admin token the service is open, and `prflagger serve` refuses to listen
anywhere but loopback in that state; that is the local-development mode.

A browser signs in once at ``/login`` and gets a cookie
``role|expiry|HMAC-SHA256(admin token, role|expiry)``. There is no session
store to lose or leak, and changing the admin token signs everyone out. Scripts
send ``Authorization: Bearer <token>`` instead. A browser POST must also come
from this origin: the cookie is SameSite=Strict, and the Origin header, when a
browser sends one, has to match.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

__all__ = ["ADMIN_ENV", "COOKIE", "VIEWER_ENV", "Tokens", "install", "websocket_role"]

ADMIN_ENV = "PRFLAGGER_ADMIN_TOKEN"
VIEWER_ENV = "PRFLAGGER_VIEWER_TOKEN"
COOKIE = "prflagger_session"
SESSION_S = 14 * 24 * 3600
MIN_TOKEN_LENGTH = 20

#: Reachable without signing in: the sign-in page itself, the stylesheet it
#: needs, and the health check a load balancer or orchestrator polls.
_OPEN_PREFIXES = ("/static/",)
_OPEN_PATHS = frozenset({"/login", "/logout", "/api/health"})
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Tokens:
    admin: str = ""
    viewer: str = ""

    @classmethod
    def from_env(cls) -> Tokens:
        tokens = cls(
            admin=os.environ.get(ADMIN_ENV, "").strip(),
            viewer=os.environ.get(VIEWER_ENV, "").strip(),
        )
        if tokens.viewer and not tokens.admin:
            raise ValueError(
                f"{VIEWER_ENV} is set but {ADMIN_ENV} is not: sessions are signed with "
                "the admin token, so a viewer token alone cannot be used"
            )
        if tokens.admin and tokens.viewer and hmac.compare_digest(tokens.admin, tokens.viewer):
            raise ValueError(f"{ADMIN_ENV} and {VIEWER_ENV} must differ")
        # Sign-in is not rate-limited, so a token must be too long to guess.
        for name, value in ((ADMIN_ENV, tokens.admin), (VIEWER_ENV, tokens.viewer)):
            if value and len(value) < MIN_TOKEN_LENGTH:
                raise ValueError(
                    f"{name} is {len(value)} characters; use at least {MIN_TOKEN_LENGTH} "
                    "(e.g. `openssl rand -hex 32`)"
                )
        return tokens

    @property
    def enabled(self) -> bool:
        return bool(self.admin)

    def role_for(self, token: str) -> str | None:
        """The role a presented token grants, compared in constant time."""
        if not token or not self.enabled:
            return None
        if hmac.compare_digest(token.encode(), self.admin.encode()):
            return "admin"
        if self.viewer and hmac.compare_digest(token.encode(), self.viewer.encode()):
            return "viewer"
        return None

    def issue(self, role: str, now: float | None = None) -> str:
        expires = int((now if now is not None else time.time()) + SESSION_S)
        body = f"{role}|{expires}"
        return f"{body}|{self._sign(body)}"

    def verify(self, cookie: str, now: float | None = None) -> str | None:
        """The role in a session cookie, if it is ours, intact and unexpired."""
        if not self.enabled or not cookie or cookie.count("|") != 2:
            return None
        role, expires, signature = cookie.split("|")
        if role not in ("admin", "viewer") or not expires.isdigit():
            return None
        if not hmac.compare_digest(signature, self._sign(f"{role}|{expires}")):
            return None
        if int(expires) < (now if now is not None else time.time()):
            return None
        if role == "viewer" and not self.viewer:
            return None  # the viewer token was withdrawn
        return role

    def _sign(self, body: str) -> str:
        return hmac.new(self.admin.encode(), body.encode(), hashlib.sha256).hexdigest()


def _role_of(tokens: Tokens, cookies: dict[str, str], authorization: str) -> str | None:
    if authorization.lower().startswith("bearer "):
        return tokens.role_for(authorization[7:].strip())
    return tokens.verify(cookies.get(COOKIE, ""))


def websocket_role(tokens: Tokens, websocket: WebSocket) -> str | None:
    """The role behind a socket handshake — or None, and the caller closes it.

    A browser sends cookies with a WebSocket handshake from any site; SameSite=Strict
    already withholds this one cross-site, and the Origin check refuses such a
    handshake outright as well.
    """
    if not tokens.enabled:
        return "admin"
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host", "")
    if origin is not None and urlsplit(origin).netloc != host:
        return None
    return _role_of(tokens, dict(websocket.cookies), websocket.headers.get("authorization", ""))


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not a browser, or a browser that does not send one for this request
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(origin).netloc == host


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def install(app: FastAPI, tokens: Tokens, render_login: Callable[..., Response]) -> None:
    """Guard every route of `app`, and add /login and /logout."""

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not tokens.enabled:
            request.state.role = "admin"
            request.state.auth = False
            return await call_next(request)
        request.state.auth = True
        path = request.url.path
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            request.state.role = None
            return await call_next(request)

        role = _role_of(tokens, dict(request.cookies), request.headers.get("authorization", ""))
        if role is None:
            if path.startswith("/api/") or request.method not in _READ_METHODS:
                return JSONResponse({"detail": "sign in required"}, status_code=401)
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        if request.method not in _READ_METHODS:
            if role != "admin":
                return JSONResponse(
                    {"detail": "the viewer token is read-only; sign in with the admin token "
                               "to change anything"},
                    status_code=403,
                )
            if not _same_origin(request):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        request.state.role = role
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> Any:  # noqa: A002
        return render_login(request, next=_safe_next(next), error="")

    @app.post("/login")
    async def login(request: Request) -> Any:
        # A URL-encoded form of two fields; the standard library reads it, so
        # signing in needs no multipart parser.
        raw = (await request.body())[:8192].decode("utf-8", "replace")
        form = {key: values[0] for key, values in parse_qs(raw).items() if values}
        token = form.get("token", "")
        target = _safe_next(form.get("next", "/"))
        role = tokens.role_for(token)
        if role is None:
            return render_login(request, next=target, error="That token is not valid.",
                                status_code=401)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE, tokens.issue(role), max_age=SESSION_S, httponly=True,
            samesite="strict", secure=_is_https(request), path="/",
        )
        return response

    @app.get("/logout")
    async def logout() -> Any:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response


def _safe_next(target: str) -> str:
    """Only ever redirect within this site after signing in."""
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
