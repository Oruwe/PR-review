"""The engine: the full pipeline, crash recovery, and the contracts that gate it."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from prflagger.core.config import Config, RepoConfig, SandboxConfig
from prflagger.core.models import (
    Adjudication,
    Citation,
    Observation,
    Outcome,
    PullRequest,
    Repo,
    Run,
    RunState,
    Suggestion,
    TestResult,
)
from prflagger.engine.recovery import recover
from prflagger.engine.scheduler import Scheduler
from prflagger.engine.states import can_transition, progress_of
from prflagger.engine.worker import RunWorker
from prflagger.probes.differential import differential_observations, outcome_observations
from prflagger.sandbox.pool import SandboxPool
from prflagger.storage.db import Database
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store
from tests.v2_fixtures import REGRESSION_TEST, build_repo


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(  # noqa: S603
        ["docker", "info"], capture_output=True, check=False
    ).returncode == 0


needs_docker = pytest.mark.skipif(
    not _docker_ready(), reason="a running Docker daemon is required"
)


# ----------------------------------------------------------------------------------
# Contracts enforced by the types themselves
# ----------------------------------------------------------------------------------


def test_an_uncited_judgement_cannot_be_constructed() -> None:
    """CLAUDE.md permits suggestions only when they cite evidence.

    Enforcing it in `__post_init__` means no code path can forget to check.
    """
    with pytest.raises(ValueError, match="at least one citation"):
        Adjudication(observation_id="o", assessment="diverges_from_repo", reasoning="because")
    with pytest.raises(ValueError, match="cite evidence"):
        Suggestion(observation_id="o", summary="do something", rationale="trust me")

    citation = Citation(type="norm", ref="needs-a-test")
    assert Adjudication(
        observation_id="o", assessment="diverges_from_repo", reasoning="r",
        citations=(citation,),
    ).citations == (citation,)


def test_a_citation_with_nothing_to_check_is_refused() -> None:
    with pytest.raises(ValueError, match="Citation.ref is required"):
        Citation(type="code", ref="")
    with pytest.raises(ValueError, match="Citation.type"):
        Citation(type="vibes", ref="somewhere")


def test_an_observation_must_say_how_we_know() -> None:
    with pytest.raises(ValueError, match="how_we_know"):
        Observation(id="o", run_id="r", kind="api_change", symbol="s",
                    what_changed="something", how_we_know="")


def test_states_only_move_forward() -> None:
    assert can_transition(RunState.QUEUED, RunState.PREPARING)
    assert can_transition(RunState.PROBING, RunState.RENDERING)     # skipping is allowed
    assert not can_transition(RunState.HEAD_RUN, RunState.BASE_RUN)  # going back is not
    assert not can_transition(RunState.DONE, RunState.PROBING)
    assert can_transition(RunState.BASE_RUN, RunState.FAILED)
    assert progress_of(RunState.DONE) == 1.0


# ----------------------------------------------------------------------------------
# Probes
# ----------------------------------------------------------------------------------


def _result(outcome: Outcome, per_test: dict[str, str]) -> TestResult:
    return TestResult(outcome, per_test, 1.0, 20, "", "")


def test_only_regressions_are_reported_not_fixes() -> None:
    """A test that starts passing is good news, and this system does not editorialise."""
    base = _result(Outcome.FAILED, {"t::a": "passed", "t::b": "failed", "t::c": "passed"})
    head = _result(Outcome.FAILED, {"t::a": "failed", "t::b": "passed"})
    kinds = {(o.kind, o.symbol) for o in differential_observations("r", base, head)}
    assert ("behavior_change", "a") in kinds       # passed -> failed
    assert ("test_removed", "c") in kinds          # vanished
    assert not any(symbol == "b" for _, symbol in kinds)  # failed -> passed is not a finding


def test_a_pre_existing_timeout_is_not_this_change_s_fault() -> None:
    slow = _result(Outcome.TIMEOUT, {})
    assert outcome_observations("r", slow, slow) == []
    fine = _result(Outcome.PASSED, {"t::a": "passed"})
    assert [o.kind for o in outcome_observations("r", fine, slow)] == ["timeout"]


# ----------------------------------------------------------------------------------
# Recovery
# ----------------------------------------------------------------------------------


def test_a_run_interrupted_by_a_restart_is_requeued(tmp_path: Path) -> None:
    """A service that runs continuously will be killed mid-run."""

    async def scenario() -> tuple[list[Run], RunState]:
        database = Database(tmp_path / "test.db")
        store = Store(database)
        bus = EventBus(database)
        bus.bind_loop(asyncio.get_running_loop())
        store.put_repo(Repo(slug="a/b", added_at=time.time()))
        store.put_run(
            Run(id="interrupted", repo="a/b", pr_number=1, base_sha="aa", head_sha="bb",
                created_at=time.time())
        )
        store.set_run_state("interrupted", RunState.HEAD_RUN)   # the kill happens here

        config = Config()
        pool = SandboxPool(config, bus)
        scheduler = Scheduler(config, store, bus, RunWorker(config, store, bus, pool))
        stranded = await recover(store, bus, scheduler)
        run = store.run("interrupted")
        assert run is not None
        return stranded, run.state

    stranded, state = asyncio.run(scenario())
    assert [r.id for r in stranded] == ["interrupted"]
    assert state is RunState.QUEUED, "an interrupted run must go back on the queue"


def test_the_same_head_is_not_verified_twice(tmp_path: Path) -> None:
    """Re-running identical code cannot change the answer; it only costs time.

    And when the head moves while a run is still queued, that queued run must be
    pointed at the new commit rather than left verifying the old one — collapsing
    a force-push storm into one run is only correct if the run that survives is
    the one for the newest code.
    """

    async def scenario() -> tuple[str | None, str | None, str | None, Run | None]:
        database = Database(tmp_path / "test.db")
        store = Store(database)
        bus = EventBus(database)
        bus.bind_loop(asyncio.get_running_loop())
        store.put_repo(Repo(slug="a/b", added_at=time.time()))
        config = Config()
        pool = SandboxPool(config, bus)
        scheduler = Scheduler(config, store, bus, RunWorker(config, store, bus, pool))

        pull = PullRequest(repo="a/b", number=1, title="t", body="", author="u",
                           base_sha="base", head_sha="head1", state="open", updated_at="now")
        first = scheduler.submit(pull)
        repeat = scheduler.submit(pull)
        from dataclasses import replace

        moved = scheduler.submit(replace(pull, head_sha="head2"))
        return first, repeat, moved, store.run(first or "")

    first, repeat, moved, run = asyncio.run(scenario())
    assert first is not None
    assert repeat is None, "the same head was queued twice"
    assert moved == first, "a force-push storm must collapse into one run"
    assert run is not None
    assert run.head_sha == "head2", (
        "the surviving run still points at the stale commit; it would verify the "
        "sha that happened to be current when it was created"
    )


# ----------------------------------------------------------------------------------
# The whole pipeline
# ----------------------------------------------------------------------------------


@needs_docker
def test_the_pipeline_finds_the_undeclared_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end on a real repository whose head commit changes an edge case.

    The PR body describes a validation fix and says nothing about the boundary
    case it also moves, or the public function it adds. Both are found, both
    cite evidence, and the run reports what it could not check.
    """
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    repo, base, head = build_repo(tmp_path / "repo")

    async def scenario() -> tuple[Run, list[Observation], dict]:
        config = Config(
            repos=(RepoConfig(slug="demo/shoplib", clone_url=str(repo),
                              package_roots=("shoplib",)),),
            sandbox=SandboxConfig(default_timeout_s=180, default_memory_mb=512),
        )
        database = Database(tmp_path / "test.db")
        store = Store(database)
        bus = EventBus(database)
        bus.bind_loop(asyncio.get_running_loop())
        store.put_repo(Repo(slug="demo/shoplib", package_roots=("shoplib",),
                            added_at=time.time()))
        pool = SandboxPool(config, bus)
        worker = RunWorker(config, store, bus, pool)
        scheduler = Scheduler(config, store, bus, worker)
        await scheduler.start(workers=1)

        pull = PullRequest(
            repo="demo/shoplib", number=1,
            title="fix: validate the discount percentage",
            body="Rejects a negative percent instead of silently ignoring it.",
            author="demo", base_sha=base, head_sha=head, state="open", updated_at="now",
        )
        store.put_pull(pull)
        identifier = scheduler.submit(pull, force=True)
        assert identifier is not None

        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            run = store.run(identifier)
            if run and run.state in (RunState.DONE, RunState.FAILED, RunState.CANCELLED):
                break
            await asyncio.sleep(1)
        await scheduler.stop()

        run = store.run(identifier)
        assert run is not None
        coverage: dict = {}
        for event in bus.since(0, run_id=identifier):
            if event.type == "run.observations":
                coverage = event.payload.get("coverage", {})
        return run, store.observations(identifier), coverage

    run, observations, coverage = asyncio.run(scenario())

    assert run.state is RunState.DONE, f"run failed: {run.error}"

    behaviour = [o for o in observations if o.kind == "behavior_change"]
    assert behaviour, "the undeclared edge-case change was not found"
    assert REGRESSION_TEST in behaviour[0].evidence_ref
    assert "passed" in behaviour[0].how_we_know and "failed" in behaviour[0].how_we_know

    added = [o for o in observations if o.kind == "api_change"]
    assert any("bulk_total" in o.symbol for o in added), "the new public function was not found"
    for observation in added:
        assert not observation.evidence_ref.startswith("/"), (
            "citations must be repo-relative, not absolute worktree paths"
        )

    # Ranking is deterministic and total.
    scores = [o.rank_score for o in observations]
    assert scores == sorted(scores, reverse=True)
    assert behaviour[0].rank_score > added[0].rank_score, (
        "a behaviour change must outrank an API addition"
    )

    # Partial verification is never presented as complete.
    assert coverage["complete"] is False
    assert any("adjudication" in entry["what"] for entry in coverage["skipped"])
    assert any("behavioural comparison" in entry for entry in coverage["verified"])

    # Every finding is placed against this repository's own charter, and the run
    # says that is what it was judged against.
    assert all(o.relevance in ("core", "supporting", "peripheral") for o in observations)
    assert behaviour[0].relevance == "core"
    assert any("demo/shoplib's own charter" in entry for entry in coverage["verified"])

    # The behaviour change matters because the repository has a test suite, and the
    # finding says so by pointing at the declared standard, not at an opinion.
    assert behaviour[0].norm_id == "declared-tests"


# ----------------------------------------------------------------------------------
# The whole pipeline, with a model reading the findings against the repository
# ----------------------------------------------------------------------------------


def _pipeline_with_model(tmp_path: Path, respond: object) -> dict[str, object]:
    """Run the fixture PR end to end with the scripted model at the network boundary."""
    from prflagger.core.config import ModelConfig
    from prflagger.llm.client import build_client
    from prflagger.llm.provider import bedrock_mantle
    from tests.model_fixture import RecordedModel, serve

    repo, base, head = build_repo(tmp_path / "repo")
    model = RecordedModel(respond)  # type: ignore[arg-type]

    async def scenario() -> dict[str, object]:
        config = Config(
            repos=(RepoConfig(slug="demo/shoplib", clone_url=str(repo),
                              package_roots=("shoplib",)),),
            sandbox=SandboxConfig(default_timeout_s=180, default_memory_mb=512),
            models=ModelConfig(),
        )
        database = Database(tmp_path / "test.db")
        store = Store(database)
        bus = EventBus(database)
        bus.bind_loop(asyncio.get_running_loop())
        store.put_repo(Repo(slug="demo/shoplib", package_roots=("shoplib",),
                            added_at=time.time()))
        client = build_client(
            config, database,
            provider=bedrock_mantle("us-east-1", base_url=model.base_url, skip_auth=True),
        )
        worker = RunWorker(config, store, bus, SandboxPool(config, bus), models=client)
        scheduler = Scheduler(config, store, bus, worker)
        await scheduler.start(workers=1)
        pull = PullRequest(
            repo="demo/shoplib", number=1, title="fix: validate the discount percentage",
            body="Rejects a negative percent instead of silently ignoring it.",
            author="demo", base_sha=base, head_sha=head, state="open", updated_at="now",
        )
        store.put_pull(pull)
        identifier = scheduler.submit(pull, force=True)
        assert identifier is not None
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            run = store.run(identifier)
            if run and run.state in (RunState.DONE, RunState.FAILED, RunState.CANCELLED):
                break
            await asyncio.sleep(1)
        await scheduler.stop()
        coverage: dict = {}
        states = []
        for event in bus.since(0, run_id=identifier):
            if event.type == "run.observations":
                coverage = event.payload.get("coverage", {})
            if event.type == "run.state":
                states.append(event.payload.get("state"))
        return {
            "run": store.run(identifier), "observations": store.observations(identifier),
            "adjudications": store.adjudications(identifier),
            "suggestions": store.suggestions(identifier), "coverage": coverage,
            "states": states, "spent": client.ledger.spent(run_id=identifier),
        }

    with serve(model):
        result = asyncio.run(scenario())
    result["requests"] = model.requests
    return result


@needs_docker
def test_findings_are_read_against_the_repository_and_every_citation_checks_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.adjudication_fixture import honest

    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    result = _pipeline_with_model(tmp_path, honest)
    run = result["run"]
    assert run is not None and run.state is RunState.DONE, run  # type: ignore[union-attr]
    assert "adjudicating" in result["states"]  # type: ignore[operator]

    adjudications = result["adjudications"]
    assert adjudications, "the findings were read against the repository"
    for adjudication in adjudications.values():  # type: ignore[union-attr]
        assert adjudication.citations, "an uncited adjudication cannot exist"
        assert adjudication.model == "anthropic.claude-sonnet-5"
    assert result["suggestions"], "a cited suggestion was kept"

    verified = result["coverage"]["verified"]  # type: ignore[index]
    assert any("every citation checked" in entry for entry in verified)
    assert result["spent"] > 0 and run.usd_spent > 0  # type: ignore[union-attr, operator]
    assert all(r["system"][0]["cache_control"] for r in result["requests"])  # type: ignore[union-attr]


@needs_docker
def test_a_model_that_fabricates_changes_nothing_but_the_coverage_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.adjudication_fixture import fabricating

    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    result = _pipeline_with_model(tmp_path, fabricating)
    assert result["run"].state is RunState.DONE  # type: ignore[union-attr]
    assert result["adjudications"] == {} and result["suggestions"] == {}
    observations = result["observations"]
    assert any(o.kind == "behavior_change" for o in observations), (  # type: ignore[union-attr]
        "the probes' findings stand on their own evidence"
    )
    skipped = result["coverage"]["skipped"]  # type: ignore[index]
    assert any("could be checked" in entry["why"] for entry in skipped)
