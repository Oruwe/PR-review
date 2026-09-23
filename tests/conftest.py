"""Shared fixtures. Everything here uses the real target repo from config.toml."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.requires import MISSING_TOOL_REASONS, missing_tool

REPO_ROOT = Path(__file__).resolve().parent.parent
TOY_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    """Keep the cache writable when the checkout is not.

    PR Flagger runs this suite on itself in its own sandbox, where the checkout
    is mounted read-only and the default `.cache` beside it cannot be created.
    Only then does the cache move to a temporary directory: a writable checkout
    keeps `.cache`, and the seeded CI job relies on the click clone it holds.
    """
    if os.environ.get("PRFLAGGER_CACHE_DIR"):
        return
    try:
        cache = Path(".cache")
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryFile(dir=cache):
            pass
    except OSError:
        os.environ["PRFLAGGER_CACHE_DIR"] = tempfile.mkdtemp(prefix="prflagger-cache-")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """A test that failed only because git or Docker is not installed is a skip.

    Most tests reach git through shared fixtures and helpers, so the absence is
    recognised where it surfaces rather than guessed per test. See `tests.requires`.
    """
    report = yield
    if report.failed and call.excinfo is not None:
        tool = missing_tool(call.excinfo.value)
        if tool is not None:
            report.outcome = "skipped"
            report.longrepr = (str(item.path), item.location[1] or 0,
                               f"Skipped: {MISSING_TOOL_REASONS[tool]}")
    return report


_AWS_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
)


@pytest.fixture(autouse=True)
def _no_ambient_model_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """No test reaches a real model because of whatever the machine happens to hold.

    The service uses a model whenever AWS credentials are present. A test that
    wants one supplies it explicitly — a provider at the network boundary, or a
    base URL pointing at a local server — so a developer's own credentials can
    neither be spent by the suite nor change what it checks.
    """
    for name in _AWS_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    nowhere = tmp_path_factory.getbasetemp() / "no-aws"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(nowhere / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(nowhere / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(scope="session")
def target() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "config.toml").read_text(encoding="utf-8"))
    return dict(data["target"])


@pytest.fixture(scope="session")
def head_sha(target: dict[str, str]) -> str:
    from prflagger.sandbox.runner import bare_clone

    bare = bare_clone(str(target["slug"]))
    completed = subprocess.run(
        ["git", f"--git-dir={bare}", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture(scope="session")
def head_worktree(target: dict[str, str], head_sha: str) -> Path:
    from prflagger.sandbox.runner import worktree_for

    return worktree_for(str(target["slug"]), head_sha).resolve()


@pytest.fixture(scope="session")
def image_key(head_worktree: Path) -> str:
    from prflagger.sandbox.runner import lockfile_image_key

    return lockfile_image_key(head_worktree)


def git_in(repo: Path, *argv: str) -> None:
    """Run a git command in a test repository."""
    subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True)


@pytest.fixture
def toy_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A real git repo whose head commit changes the body of `c()`."""
    repo = tmp_path / "toy"
    repo.mkdir()
    shutil.copytree(TOY_FIXTURES / "toypkg", repo / "toypkg")

    git_in(repo, "init", "--quiet")
    git_in(repo, "config", "user.email", "test@example.com")
    git_in(repo, "config", "user.name", "Test")
    git_in(repo, "add", ".")
    git_in(repo, "commit", "--quiet", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    leaf = repo / "toypkg" / "leaf.py"
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace("doubled = value * 2", "doubled = value * 3"),
        encoding="utf-8",
    )
    git_in(repo, "commit", "--quiet", "-am", "change c()")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return repo, base, head
