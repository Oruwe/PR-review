"""Storage: the schema, the typed layer, and the event log the live UI rides on."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from prflagger.core.ids import observation_id, run_id
from prflagger.core.models import (
    Adjudication,
    Citation,
    Observation,
    PullRequest,
    Repo,
    Run,
    RunState,
    Suggestion,
)
from prflagger.storage.db import Database
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store


@pytest.fixture
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def test_schema_applies_and_is_idempotent(database: Database) -> None:
    """Applying twice must be a no-op — migration runs on every boot."""
    first = database.migrate()
    second = database.migrate()
    assert first == second
    tables = {
        row[0]
        for row in database.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runs", "jobs", "observations", "events", "llm_spend"} <= tables
    assert database.scalar("PRAGMA journal_mode") == "wal"


def test_run_lifecycle_and_recovery_query(database: Database) -> None:
    """A run in flight is what boot recovery has to find; a finished one is not."""
    store = Store(database)
    store.put_repo(Repo(slug="a/b", added_at=time.time()))
    identifier = run_id()
    store.put_run(
        Run(id=identifier, repo="a/b", pr_number=3, base_sha="aa", head_sha="bb",
            created_at=time.time())
    )
    store.set_run_state(identifier, RunState.HEAD_RUN)
    assert [r.id for r in store.unfinished_runs()] == [identifier]

    store.set_run_state(identifier, RunState.DONE)
    assert store.unfinished_runs() == []
    finished = store.run(identifier)
    assert finished is not None
    assert finished.finished_at is not None
    assert finished.started_at is not None


def test_observations_round_trip_with_their_annotations(database: Database) -> None:
    store = Store(database)
    store.put_repo(Repo(slug="a/b", added_at=time.time()))
    identifier = run_id()
    store.put_run(
        Run(id=identifier, repo="a/b", pr_number=1, base_sha="aa", head_sha="bb",
            created_at=time.time())
    )

    observation = Observation(
        id=observation_id(identifier, "api_change", "pkg.f", "pkg/x.py:1"),
        run_id=identifier, kind="api_change", symbol="pkg.f",
        what_changed="added", how_we_know="pkg/x.py:1", rank_score=0.4,
    )
    store.put_observations([observation])
    assert [o.id for o in store.observations(identifier)] == [observation.id]

    citation = Citation(type="code", ref="pkg/y.py:9", quote="def f(): ...")
    store.put_adjudication(
        Adjudication(observation_id=observation.id, assessment="diverges_from_repo",
                     reasoning="unlike its siblings", citations=(citation,))
    )
    store.put_suggestion(
        Suggestion(observation_id=observation.id, summary="add a test",
                   rationale="every sibling has one", citations=(citation,))
    )
    back = store.adjudications(identifier)[observation.id]
    assert back.citations[0].ref == "pkg/y.py:9"
    assert store.suggestions(identifier)[observation.id].summary == "add a test"


def test_pulls_track_the_latest_head(database: Database) -> None:
    store = Store(database)
    store.put_repo(Repo(slug="a/b", added_at=time.time()))
    for head in ("aaa", "bbb"):
        store.put_pull(
            PullRequest(repo="a/b", number=7, title="t", body="", author="u",
                        base_sha="base", head_sha=head, state="open", updated_at="now")
        )
    pull = store.pull("a/b", 7)
    assert pull is not None
    assert pull.head_sha == "bbb"
    assert len(store.pulls("a/b")) == 1


def test_caller_counts_feed_ranking(database: Database) -> None:
    store = Store(database)
    store.put_call_edges("a/b", {"one": {"target"}, "two": {"target"}, "three": {"other"}})
    assert store.caller_counts("a/b") == {"target": 2, "other": 1}


# ----------------------------------------------------------------------------------
# The event log
# ----------------------------------------------------------------------------------


def test_events_are_filtered_by_run_and_repo(database: Database) -> None:
    bus = EventBus(database)
    bus.emit("run.state", run_id="r1", repo="a/b", state="queued")
    bus.emit("run.state", run_id="r2", repo="a/b", state="queued")
    bus.emit("repo.added", repo="c/d")

    assert [e.type for e in bus.since(0, run_id="r1")] == ["run.state"]
    assert len(bus.since(0, repo="a/b")) == 2
    assert bus.head() == 3


def test_replay_then_live_delivers_each_event_exactly_once(database: Database) -> None:
    """The property the whole live view depends on.

    A client subscribes first, replays the backlog, then drains what arrived
    during the replay — discarding anything at or below what it already sent.
    Opening the page late must show the same transcript as watching from the
    start, with nothing missing and nothing doubled.
    """

    async def scenario() -> tuple[list[str], list[str]]:
        bus = EventBus(database)
        bus.bind_loop(asyncio.get_running_loop())

        # A run that happened before anyone was watching.
        for index in range(5):
            bus.emit("log.line", run_id="r1", text=f"early {index}")

        watcher = bus.subscribe(run_id="r1")          # subscribe BEFORE reading
        backlog = bus.since(0, run_id="r1")
        # Events arriving while the backlog is being read must not be lost.
        for index in range(3):
            bus.emit("log.line", run_id="r1", text=f"during {index}")

        delivered = [e.payload["text"] for e in backlog]
        highest = backlog[-1].seq

        while not watcher.queue.empty():
            event = await watcher.queue.get()
            if event.seq <= highest:
                continue                               # already replayed
            delivered.append(event.payload["text"])

        for index in range(2):
            bus.emit("log.line", run_id="r1", text=f"late {index}")
        while not watcher.queue.empty():
            delivered.append((await watcher.queue.get()).payload["text"])

        watcher.close()
        expected = (
            [f"early {i}" for i in range(5)]
            + [f"during {i}" for i in range(3)]
            + [f"late {i}" for i in range(2)]
        )
        return delivered, expected

    delivered, expected = asyncio.run(scenario())
    assert delivered == expected
    assert len(delivered) == len(set(delivered)), "an event was delivered twice"


def test_pruning_keeps_events_for_runs_still_in_flight(database: Database) -> None:
    """A long run must never lose the start of its own transcript."""
    store = Store(database)
    store.put_repo(Repo(slug="a/b", added_at=time.time()))
    store.put_run(
        Run(id="live", repo="a/b", pr_number=1, base_sha="aa", head_sha="bb",
            created_at=time.time())
    )
    store.put_run(
        Run(id="over", repo="a/b", pr_number=2, base_sha="cc", head_sha="dd",
            created_at=time.time())
    )
    store.set_run_state("over", RunState.DONE)

    bus = EventBus(database)
    bus.emit("log.line", run_id="live", text="from a run still going")
    bus.emit("log.line", run_id="over", text="from a finished run")
    database.execute("UPDATE events SET ts = ?", (time.time() - 40 * 86400,))

    bus.prune(older_than_days=7)
    remaining = {e.run_id for e in bus.since(0)}
    assert remaining == {"live"}
