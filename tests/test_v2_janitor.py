"""Disk reclamation: what goes, what is kept, and what refuses to start without room."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from prflagger.core.config import Config, ServerConfig
from prflagger.core.models import Repo, Run, RunState
from prflagger.engine.janitor import MIN_FREE_RATIO, Reclaimed, disk_free_ratio, sweep
from prflagger.storage.db import Database
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Store, EventBus, Config]:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    database = Database(tmp_path / "j.db")
    store = Store(database)
    store.put_repo(Repo(slug="demo/lib", added_at=time.time()))
    return store, EventBus(database), Config(server=ServerConfig(event_retention_days=7))


def _run(store: Store, run_id: str, *, age_days: float, head: str) -> None:
    created = time.time() - age_days * 86400
    store.put_run(
        Run(id=run_id, repo="demo/lib", pr_number=1, base_sha="base" + head,
            head_sha=head, created_at=created)
    )
    store.set_run_state(run_id, RunState.DONE)
    store.db.execute("UPDATE runs SET created_at = ? WHERE id = ?", (created, run_id))


def _transcript(cache: Path, run_id: str, text: str = "x" * 4096) -> Path:
    directory = cache / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run_id}-base_run.ndjson").write_text(text, encoding="utf-8")
    return directory


def _worktree(cache: Path, sha: str, text: str = "y" * 4096) -> Path:
    tree = cache / "worktrees" / "demo__lib" / sha
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "file.py").write_text(text, encoding="utf-8")
    return tree


def test_stale_artifacts_go_and_recent_ones_stay(
    wired: tuple[Store, EventBus, Config], tmp_path: Path
) -> None:
    """The whole point: reclaim what nothing needs, keep what something does."""
    store, bus, config = wired
    cache = tmp_path / "cache"

    _run(store, "old", age_days=40, head="aaa")
    _run(store, "new", age_days=0.1, head="bbb")
    old_transcript = _transcript(cache, "old")
    new_transcript = _transcript(cache, "new")
    old_tree = _worktree(cache, "aaa")
    new_tree = _worktree(cache, "bbb")

    reclaimed = sweep(store, config, bus, keep_days=7)

    assert not old_transcript.exists(), "a transcript for a long-finished run was kept"
    assert new_transcript.exists(), "a recent run's transcript was deleted"
    assert not old_tree.exists(), "a worktree no recent run refers to was kept"
    assert new_tree.exists(), "a worktree a recent run refers to was deleted"
    assert reclaimed.transcripts == 1
    assert reclaimed.worktrees == 1
    assert reclaimed.bytes_freed >= 8192


def test_a_run_still_in_flight_keeps_its_transcript(
    wired: tuple[Store, EventBus, Config], tmp_path: Path
) -> None:
    """A long run must not have the start of its own transcript swept out from under it."""
    store, bus, config = wired
    cache = tmp_path / "cache"
    created = time.time() - 40 * 86400
    store.put_run(
        Run(id="running", repo="demo/lib", pr_number=2, base_sha="b", head_sha="h",
            created_at=created)
    )
    store.set_run_state("running", RunState.HEAD_RUN)
    store.db.execute("UPDATE runs SET created_at = ? WHERE id = ?", (created, "running"))
    transcript = _transcript(cache, "running")

    sweep(store, config, bus, keep_days=7)
    assert transcript.exists(), "an in-flight run's transcript was reclaimed"


def test_dry_run_reports_without_removing(
    wired: tuple[Store, EventBus, Config], tmp_path: Path
) -> None:
    store, bus, config = wired
    cache = tmp_path / "cache"
    _run(store, "old", age_days=40, head="aaa")
    transcript = _transcript(cache, "old")
    tree = _worktree(cache, "aaa")

    reclaimed = sweep(store, config, bus, keep_days=7, dry_run=True)
    assert transcript.exists() and tree.exists(), "dry run deleted something"
    assert reclaimed.worktrees == 1 and reclaimed.transcripts == 1
    assert reclaimed.bytes_freed > 0


def test_sweeping_an_empty_cache_is_harmless(
    wired: tuple[Store, EventBus, Config]
) -> None:
    store, bus, config = wired
    assert sweep(store, config, bus, keep_days=7) == Reclaimed()


def test_disk_headroom_is_a_fraction() -> None:
    ratio = disk_free_ratio()
    assert 0.0 <= ratio <= 1.0
    assert MIN_FREE_RATIO > 0


def test_a_job_refuses_to_start_without_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing at the start with a clear reason beats failing halfway with a cryptic one."""
    import asyncio

    from prflagger.core.models import Outcome
    from prflagger.lang.python import PYTHON
    from prflagger.sandbox import pool as pool_module
    from prflagger.sandbox.pool import JobSpec, SandboxPool

    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(pool_module, "disk_free_ratio", lambda *a, **k: 0.001)

    database = Database(tmp_path / "p.db")
    sandbox = SandboxPool(Config(), EventBus(database))
    spec = JobSpec(
        run_id="rDISK", job_id="rDISK-j", stage="head_run", repo_path=tmp_path,
        commit="c", toolchain=PYTHON, command=("pytest",),
    )
    result = asyncio.run(sandbox.run(spec))
    assert result.outcome is Outcome.INSTALL_FAILED
    assert "0.1% of the disk is free" in result.stderr
    assert "prflagger gc" in result.stderr
