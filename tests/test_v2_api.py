"""The web surface: REST, the WebSocket's replay-then-live contract, and the views."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prflagger.api.app import create_app
from prflagger.api.service import Service
from prflagger.core.config import Config, RepoConfig
from prflagger.core.models import Observation, PullRequest, Repo, Run, RunState
from prflagger.storage.db import Database


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Service:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    config = Config(repos=(RepoConfig(slug="demo/lib", clone_url=str(tmp_path / "src"),
                                      package_roots=("lib",)),))
    return Service.build(config, db_path=tmp_path / "api.db")


@pytest.fixture
def client(service: Service) -> TestClient:
    # watch=False: these tests must not reach out to GitHub.
    return TestClient(create_app(service, config=service.config, watch=False))


def _seed(service: Service) -> str:
    service.store.put_repo(Repo(slug="demo/lib", package_roots=("lib",), added_at=time.time()))
    service.store.put_pull(
        PullRequest(repo="demo/lib", number=4, title="a change", body="body",
                    author="someone", base_sha="a" * 40, head_sha="b" * 40,
                    state="open", updated_at="2026-01-01T00:00:00Z",
                    additions=12, deletions=3)
    )
    run = Run(id="rAPI", repo="demo/lib", pr_number=4, base_sha="a" * 40,
              head_sha="b" * 40, created_at=time.time())
    service.store.put_run(run)
    service.store.put_observations([
        Observation(id="rAPI-o1", run_id="rAPI", kind="behavior_change",
                    symbol="test_thing", what_changed="passed at base, failed at head",
                    how_we_know="nodeid t.py::test_thing: passed -> failed",
                    evidence_ref="t.py::test_thing", severity=1.0, confidence=0.9,
                    rank_score=0.9),
        Observation(id="rAPI-o2", run_id="rAPI", kind="lint_regression",
                    symbol="lib/x.py", what_changed="ruff reports F401 at head",
                    how_we_know="ruff F401 in lib/x.py: unused import",
                    evidence_ref="lib/x.py:F401", severity=0.3, confidence=0.85,
                    rank_score=0.2),
    ])
    service.bus.emit(
        "run.observations", run_id="rAPI", repo="demo/lib", count=2,
        coverage={"verified": ["behavioural comparison over 5 tests"],
                  "skipped": [{"what": "adjudication", "why": "no model provider"}],
                  "complete": False},
    )
    service.store.set_run_state("rAPI", RunState.DONE)
    return "rAPI"


def test_health_reports_what_the_service_is_doing(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["sandbox_capacity"] >= 1
    assert "queue_depth" in payload and "event_head" in payload


def test_every_view_renders(client: TestClient, service: Service) -> None:
    run_id = _seed(service)
    for path in ("/", "/repo/demo/lib", "/repo/demo/lib/prs",
                 f"/runs/{run_id}", f"/runs/{run_id}/report"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "text/html" in response.headers["content-type"]


def test_the_report_shows_all_four_contract_fields(
    client: TestClient, service: Service
) -> None:
    """SPEC.md § C10: every finding must show what changed, how we know, the norm
    it relates to, and a confidence."""
    run_id = _seed(service)
    html = client.get(f"/runs/{run_id}/report").text
    for label in ("What changed", "How we know", "Repo standard", "Confidence"):
        assert label in html, f"the report omits {label!r}"
    assert "passed at base, failed at head" in html
    assert "nodeid t.py::test_thing" in html
    # And it never renders a verdict.
    for forbidden in (">approve<", ">reject<", "LGTM", "looks risky"):
        assert forbidden.lower() not in html.lower()


def test_partial_verification_is_labelled_as_partial(
    client: TestClient, service: Service
) -> None:
    run_id = _seed(service)
    html = client.get(f"/runs/{run_id}/report").text
    assert "partial" in html
    assert "no model provider" in html
    payload = client.get(f"/api/runs/{run_id}/report").json()
    assert payload["coverage"]["complete"] is False


def test_pull_requests_are_ranked_by_weight_not_count(
    client: TestClient, service: Service
) -> None:
    _seed(service)
    rows = client.get("/api/repos/demo/lib/pulls").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["findings"] == 2
    # 0.9 + 0.2: one behaviour change dominates, which is the point of weighting.
    assert row["risk"] == pytest.approx(1.1, abs=0.001)
    assert row["by_kind"] == {"behavior_change": 1, "lint_regression": 1}


def test_unknown_things_404_in_the_right_format(client: TestClient) -> None:
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/runs/nope").status_code == 404
    assert "text/html" in client.get("/runs/nope").headers["content-type"]


def test_a_run_cannot_be_triggered_for_an_unknown_pull(client: TestClient) -> None:
    response = client.post("/api/runs", json={"repo": "demo/lib", "pr": 999})
    assert response.status_code == 404
    assert "poll the repo first" in response.text


def _write_transcript(run_id: str, job: str, texts: list[str]) -> None:
    """Write a per-job transcript the way the sandbox does."""
    from prflagger.core.config import cache_root

    directory = cache_root() / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{job}.ndjson").open("a", encoding="utf-8") as handle:
        for index, text in enumerate(texts, start=1):
            handle.write(json.dumps({
                "stream": "stdout", "seq": index, "offset_ms": index * 100, "text": text,
            }) + "\n")


def test_log_lines_are_never_written_to_the_events_table(
    client: TestClient, service: Service
) -> None:
    """The one unbounded dimension must not live in the database.

    A suite printing fifty thousand lines would otherwise write fifty thousand
    rows for a single job, against a database with one writer.
    """
    run_id = _seed(service)
    before = int(service.db.scalar("SELECT COUNT(*) FROM events", default=0) or 0)
    for index in range(500):
        service.bus.publish("log.line", run_id=run_id, stream="stdout",
                            text=f"line {index}", offset_ms=index)
    after = int(service.db.scalar("SELECT COUNT(*) FROM events", default=0) or 0)
    assert after == before, f"{after - before} log lines leaked into the events table"


def test_the_socket_replays_the_backlog_then_goes_live(
    client: TestClient, service: Service
) -> None:
    """A tab opened at the end of a run must see what a tab opened at the start saw.

    Two sources are replayed: the event rows, and the transcript on disk. This
    asserts they arrive together and in order.
    """
    run_id = _seed(service)
    _write_transcript(run_id, f"{run_id}-base_run", [f"line {i}" for i in range(4)])

    with client.websocket_connect(f"/ws/runs/{run_id}?cursor=0") as socket:
        replayed: list[dict] = []
        while True:
            frame = socket.receive_json()
            if frame["type"] == "batch":
                replayed.extend(frame["events"])
            elif frame["type"] == "live":
                break
        texts = [e["text"] for e in replayed if e["type"] == "log.line"]
        assert texts == ["line 0", "line 1", "line 2", "line 3"]
        # The row-backed records replay alongside the transcript.
        assert any(e["type"] == "run.observations" for e in replayed)

        # Now live: a line published after the handshake arrives, exactly once.
        service.bus.publish("log.line", run_id=run_id, stream="stdout", text="live one")
        frame = socket.receive_json()
        assert frame["type"] == "event"
        assert frame["event"]["text"] == "live one"


def test_a_reconnect_resumes_the_transcript_instead_of_repeating_it(
    client: TestClient, service: Service
) -> None:
    """A dropped socket must not replay lines the client already rendered."""
    run_id = _seed(service)
    _write_transcript(run_id, f"{run_id}-base_run", [f"line {i}" for i in range(6)])

    def drain(query: str) -> tuple[list[str], int]:
        with client.websocket_connect(f"/ws/runs/{run_id}?{query}") as socket:
            seen: list[str] = []
            while True:
                frame = socket.receive_json()
                if frame["type"] == "batch":
                    seen += [e["text"] for e in frame["events"] if e["type"] == "log.line"]
                elif frame["type"] == "live":
                    return seen, frame["lines"]

    first, line_cursor = drain("cursor=0&lines=0")
    assert first == [f"line {i}" for i in range(6)]
    assert line_cursor == 6

    # The client reconnects having rendered three of them.
    resumed, _ = drain("cursor=0&lines=3")
    assert resumed == ["line 3", "line 4", "line 5"], (
        "a reconnect re-sent lines the client already had"
    )


def test_the_socket_only_carries_its_own_run(client: TestClient, service: Service) -> None:
    run_id = _seed(service)
    service.bus.emit("log.line", run_id="other", text="not mine")
    service.bus.emit("log.line", run_id=run_id, text="mine")
    with client.websocket_connect(f"/ws/runs/{run_id}?cursor=0") as socket:
        seen: list[str] = []
        while True:
            frame = socket.receive_json()
            if frame["type"] == "batch":
                seen.extend(e.get("text", "") for e in frame["events"])
            elif frame["type"] == "live":
                break
    assert "mine" in seen
    assert "not mine" not in seen


def test_the_budget_endpoint_reports_remaining_credit(
    client: TestClient, service: Service
) -> None:
    Database(service.db.path).execute(
        "INSERT INTO llm_spend (ts, model, stage, usd) VALUES (?, ?, ?, ?)",
        (time.time(), "anthropic.claude-haiku-4-5", "adjudicate", 0.0123),
    )
    payload = client.get("/api/budget").json()
    assert payload["spent_usd"] == pytest.approx(0.0123)
    assert payload["remaining_usd"] == pytest.approx(payload["total_usd"] - 0.0123)
    assert payload["calls"] == 1


# ----------------------------------------------------------------------------------
# The repository's memory, and the banner that interrupts for a major update
# ----------------------------------------------------------------------------------


def _remember(service: Service) -> None:
    from prflagger.core.models import Charter, Claim

    service.store.put_repo(Repo(slug="demo/lib", package_roots=("lib",), added_at=time.time()))
    service.store.put_charter(Charter(
        repo="demo/lib", sha="c" * 40, name="lib", summary="Parse config files safely.",
        claims=(
            Claim("purpose", "Parse config files without executing them", "README.md:3"),
            Claim("constraint", "Never import user code", "CONTRIBUTING.md:9"),
        ),
        version="1.0.0", toolchain="python", entry_points=("lib = lib.cli:main",),
    ))


def test_the_charter_is_served_with_its_sources(client: TestClient, service: Service) -> None:
    assert client.get("/api/repos/demo/lib/charter").status_code == 404
    _remember(service)
    charter = client.get("/api/repos/demo/lib/charter").json()
    assert charter["summary"] == "Parse config files safely."
    assert {c["source"] for c in charter["claims"]} == {"README.md:3", "CONTRIBUTING.md:9"}
    atlas = client.get("/repo/demo/lib").text
    assert "What this repository is for" in atlas
    assert "Never import user code" in atlas and "CONTRIBUTING.md:9" in atlas


def test_a_major_update_stays_on_every_page_until_acknowledged(
    client: TestClient, service: Service
) -> None:
    run_id = _seed(service)
    _remember(service)
    service.notifier.raise_(repo="demo/lib", kind="repo.update", level="notable",
                            title="Notable update to demo/lib", body="b", sha="n" * 40)
    notice = service.notifier.raise_(
        repo="demo/lib", kind="repo.update", level="major",
        title="Major update to demo/lib: its stated purpose changed", body="b", sha="d" * 40,
    )
    assert notice is not None
    pages = ("/", "/repo/demo/lib", "/repo/demo/lib/prs", "/repo/demo/lib/changes",
             f"/runs/{run_id}", f"/runs/{run_id}/report")
    for path in pages:
        html = client.get(path).text
        assert "its stated purpose changed" in html, f"banner missing on {path}"
        assert "Notable update" not in html or path.endswith("/changes"), (
            f"a notable update should not interrupt {path}"
        )

    assert len(client.get("/api/notifications").json()) == 2
    majors = client.get("/api/notifications?level=major").json()
    assert [n["level"] for n in majors] == ["major"]
    assert client.post(f"/api/notifications/{notice.id}/ack").json() == {"acknowledged": True}
    assert client.post(f"/api/notifications/{notice.id}/ack").json() == {"acknowledged": False}
    for path in pages:
        assert "its stated purpose changed" not in client.get(path).text or path.endswith(
            "/changes"
        ), f"banner survived acknowledgement on {path}"
    assert client.get("/api/notifications?level=bogus").status_code == 400
    history = client.get("/repo/demo/lib/changes").text
    assert "its stated purpose changed" in history and "acknowledged" in history, (
        "an acknowledged update must stay in the repository's history"
    )


def test_the_report_says_what_it_judged_against(client: TestClient, service: Service) -> None:
    run_id = _seed(service)
    _remember(service)
    service.store.set_run_charter_impact(run_id, "major")
    service.bus.emit("run.charter_impact", run_id=run_id, repo="demo/lib", level="major",
                     signals=[{"kind": "entry_points", "level": "major",
                               "detail": "1 way(s) of running the project were removed",
                               "evidence": "lib = lib.cli:main"}])
    payload = client.get(f"/api/runs/{run_id}/report").json()
    assert payload["charter_impact"]["level"] == "major"
    assert payload["charter"]["summary"] == "Parse config files safely."
    html = client.get(f"/runs/{run_id}/report").text
    assert "ways of running the project were removed" in html or "1 way(s)" in html
    rows = client.get("/api/repos/demo/lib/pulls").json()
    assert rows[0]["charter_impact"] == "major"
