"""Letting git read private repositories without the token ever touching disk.

`git clone https://github.com/owner/private` fails without credentials, and the
usual fixes all leave the token somewhere it should not be: in the remote URL
(and so in `.git/config`), on the command line (visible to anyone running
`ps`), or in a credential helper's store.

Instead the token travels as an HTTP header set through git's
``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` environment
variables (git 2.31+). It exists only in the environment of the one git process
that needs it, and only for URLs on the GitHub host the service is configured
for — never for any other remote.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import structlog

from prflagger.vcs.github import token_from_env

__all__ = ["git_env", "run_git", "web_base"]

log = structlog.get_logger(__name__)

#: What git prints when a server refused the credentials it was given.
_REFUSED = re.compile(
    r"could not read Username|Authentication failed|Invalid username or password|"
    r"returned error: 40[13]|terminal prompts disabled",
    re.IGNORECASE,
)

_API_VAR = "PRFLAGGER_GITHUB_API"


def web_base() -> tuple[str, str]:
    """(scheme, host) that repository URLs use on the configured GitHub.

    ``https://api.github.com`` serves github.com; an Enterprise Server's API at
    ``https://ghe.example/api/v3`` serves ``https://ghe.example``.
    """
    api = os.environ.get(_API_VAR, "").strip()
    if not api:
        return "https", "github.com"
    parts = urlsplit(api)
    if parts.netloc == "api.github.com":
        return "https", "github.com"
    return parts.scheme or "https", parts.netloc


def git_env(url: str | None) -> dict[str, str] | None:
    """The environment for a git command that talks to `url`, or None for the default.

    Adds the token only when there is one and `url` is on the configured GitHub
    host; a local path, another host, or no token leaves git's environment alone.
    """
    token = token_from_env()
    if not token or not url:
        return None
    scheme, host = web_base()
    parts = urlsplit(url)
    if parts.scheme != scheme or parts.netloc != host:
        return None
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    index = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    env["GIT_CONFIG_COUNT"] = str(index + 1)
    env[f"GIT_CONFIG_KEY_{index}"] = f"http.{scheme}://{host}/.extraheader"
    env[f"GIT_CONFIG_VALUE_{index}"] = f"AUTHORIZATION: basic {basic}"
    return env


def run_git(
    argv: Sequence[str],
    *,
    remote: str | None,
    timeout_s: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git, with the token when `remote` is on the GitHub host.

    If the server refuses those credentials — a token that expired, was revoked,
    or was never granted this repository — the command is run once more without
    them. A public repository then still clones; a private one fails with git's
    own message, which names the problem.
    """
    env = git_env(remote)
    completed = subprocess.run(  # noqa: S603
        list(argv), capture_output=True, text=True, check=False, timeout=timeout_s,
        env=env, cwd=cwd,
    )
    if env is not None and completed.returncode != 0 and _REFUSED.search(completed.stderr):
        log.warning("git.token_refused", remote=remote, action="retrying anonymously")
        completed = subprocess.run(  # noqa: S603
            list(argv), capture_output=True, text=True, check=False, timeout=timeout_s,
            cwd=cwd,
        )
    return completed
