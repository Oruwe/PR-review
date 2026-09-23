"""The repository's memory of itself: what goes in it, how it moves, who is told.

Real git repositories, a real HTTP server for the webhook, a real database. The
properties worth asserting — that every remembered claim traces to a real line,
that one repository's memory never reaches another, that a major update is
announced once and loudly — are exactly what a mock would assert into existence.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from prflagger.charter.drift import compare
from prflagger.charter.extract import extract_charter
from prflagger.charter.keeper import CharterKeeper
from prflagger.charter.relevance import WEIGHT, classify, grounded
from prflagger.core.config import CharterConfig, Config, RepoConfig
from prflagger.core.models import Charter, Claim, Observation, Repo
from prflagger.engine.notify import Notifier
from prflagger.lang.base import Toolchain
from prflagger.lang.detect import pack
from prflagger.storage.db import Database
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store


def _pack(name: str) -> Toolchain:
    toolchain = pack(name)
    assert toolchain is not None, name
    return toolchain


PYTHON = _pack("python")

_PYPROJECT = """\
[project]
name = "invoicer"
version = "1.4.0"
description = "Generate and validate invoices for small businesses."
requires-python = ">=3.10"
license = {text = "MIT"}
dependencies = ["jinja2>=3", "pydantic"]

[project.scripts]
invoicer = "invoicer.cli:main"
"""

_README = """\
# invoicer

Generate and validate invoices for small businesses, entirely offline.

## Who it is for

- Bookkeepers at firms with fewer than fifty staff

## Features

- Render invoices to PDF from a template
- Validate VAT numbers and totals
  before anything is sent

## Non-goals

- Payment processing of any kind
"""


def _git(repo: Path, *argv: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True
    ).stdout.strip()


def _invoicer(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "T")
    (root / "pyproject.toml").write_text(_PYPROJECT)
    (root / "README.md").write_text(_README)
    package = root / "invoicer"
    package.mkdir()
    (package / "__init__.py").write_text('"""Invoice generation and validation."""\n')
    (package / "render.py").write_text(
        '"""Render invoices to documents."""\n\ndef render(i):\n    return i\n\n'
        "def render_batch(i):\n    return i\n"
    )
    (package / "validate.py").write_text(
        '"""Validate invoice totals and tax identifiers."""\n\ndef validate(i):\n'
        "    return True\n\ndef validate_vat(n):\n    return True\n"
    )
    (package / "cli.py").write_text(
        '"""Command line entry point."""\n\ndef main():\n    return 0\n'
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _charter(root: Path, slug: str = "acme/invoicer", sha: str = "s1") -> Charter:
    return extract_charter(root, slug=slug, sha=sha, toolchain=PYTHON)


# ----------------------------------------------------------------------------------
# What goes into the memory
# ----------------------------------------------------------------------------------


def test_the_charter_captures_goal_target_function_and_rules(tmp_path: Path) -> None:
    charter = _charter(_invoicer(tmp_path / "r"))
    assert charter.name == "invoicer"
    assert charter.summary == "Generate and validate invoices for small businesses."
    assert charter.version == "1.4.0" and charter.license == "MIT"
    assert charter.entry_points == ("invoicer = invoicer.cli:main",)
    assert charter.dependencies == ("jinja2", "pydantic")
    assert "invoicer.validate.validate_vat" in charter.public_api

    texts = {kind: [c.text for c in charter.claims_of(kind)] for kind in Claim.KINDS}
    assert any("offline" in t for t in texts["purpose"]), "the README's own goal is missing"
    assert "Python >=3.10" in texts["target"]
    assert "Bookkeepers at firms with fewer than fifty staff" in texts["target"]
    assert any("PDF" in t for t in texts["capability"])
    assert any(
        t.startswith("invoicer.validate: Validate invoice totals") for t in texts["capability"]
    )
    assert "Not a goal: Payment processing of any kind" in texts["constraint"], (
        "a non-goal quoted bare reads as a feature"
    )


def test_a_wrapped_bullet_is_remembered_whole(tmp_path: Path) -> None:
    """Half a sentence stored as the repository's claim is a misquote."""
    charter = _charter(_invoicer(tmp_path / "r"))
    assert "Validate VAT numbers and totals before anything is sent" in [
        c.text for c in charter.claims
    ]


def test_every_claim_cites_a_line_that_exists(tmp_path: Path) -> None:
    """The whole guarantee: nothing in the memory the repository did not say."""
    root = _invoicer(tmp_path / "r")
    charter = _charter(root)
    assert charter.claims
    for claim in charter.claims:
        path, line = claim.source.rsplit(":", 1)
        lines = (root / path).read_text().splitlines()
        assert 1 <= int(line) <= len(lines), f"{claim.source} does not exist"


def test_a_claim_without_a_source_cannot_exist() -> None:
    with pytest.raises(ValueError, match="invention"):
        Claim(kind="purpose", text="does everything", source="")


def test_other_ecosystems_describe_themselves_too(tmp_path: Path) -> None:
    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text(json.dumps({
        "name": "fastqueue", "version": "3.1.0", "description": "A tiny job queue.",
        "license": "Apache-2.0", "bin": {"fq": "bin/fq.js"},
        "engines": {"node": ">=20"}, "dependencies": {"ioredis": "^5"},
    }, indent=2))
    charter = extract_charter(
        node, slug="x/fastqueue", sha="s", toolchain=_pack("node")
    )
    assert charter.summary == "A tiny job queue."
    assert charter.entry_points == ("fq = bin/fq.js",)
    assert charter.dependencies == ("ioredis",)
    assert "node >=20" in [c.text for c in charter.claims_of("target")]

    go = tmp_path / "go"
    (go / "cmd" / "server").mkdir(parents=True)
    (go / "cmd" / "server" / "main.go").write_text("package main\n")
    (go / "go.mod").write_text(
        "module example.com/svc\n\ngo 1.23\n\nrequire (\n\tgithub.com/x/y v1.0.0\n)\n"
    )
    charter = extract_charter(go, slug="x/svc", sha="s", toolchain=_pack("go"))
    assert charter.name == "example.com/svc"
    assert charter.entry_points == ("server = cmd/server",)
    assert charter.dependencies == ("github.com/x/y",)


# ----------------------------------------------------------------------------------
# How far it moved
# ----------------------------------------------------------------------------------


def test_drift_is_graded_by_what_changed_not_how_much(tmp_path: Path) -> None:
    base = _charter(_invoicer(tmp_path / "r"))
    assert compare(base, replace(base, sha="s2")).level == "none"
    assert compare(base, replace(base, sha="s2", summary=base.summary + ".")).level == "minor"
    assert compare(base, replace(base, sha="s2", version="2.0.0")).level == "major"
    assert compare(base, replace(base, sha="s2", entry_points=())).level == "major"
    assert compare(base, replace(base, sha="s2", toolchain="node")).level == "major"
    fewer = replace(base, sha="s2", public_api=base.public_api[1:])
    assert compare(base, fewer).level == "major", "a fifth of the API removed is major"
    grown = replace(base, sha="s2", dependencies=(*base.dependencies, "httpx"))
    assert compare(base, grown).level == "minor"


def test_dropping_a_non_goal_is_named_for_what_it_means(tmp_path: Path) -> None:
    base = _charter(_invoicer(tmp_path / "r"))
    kept = tuple(c for c in base.claims if "Payment processing" not in c.text)
    drift = compare(base, replace(base, sha="s2", claims=kept))
    (signal,) = drift.signals
    assert (signal.kind, signal.level) == ("non_goals", "notable")
    assert signal.detail.startswith("1 stated non-goal dropped")
    assert signal.evidence == "Payment processing of any kind"
    narrowed = compare(replace(base, claims=kept), replace(base, sha="s2"))
    assert narrowed.level == "minor", "promising to do less only narrows the repository"


def test_thresholds_are_configuration(tmp_path: Path) -> None:
    base = _charter(_invoicer(tmp_path / "r"))
    fewer = replace(base, sha="s2", public_api=base.public_api[1:])
    lenient = CharterConfig(api_removed_major_fraction=0.9, api_removed_major_count=50)
    assert compare(base, fewer, lenient).level == "notable"


def test_two_repositories_are_never_compared(tmp_path: Path) -> None:
    base = _charter(_invoicer(tmp_path / "r"))
    with pytest.raises(ValueError, match="different repositories"):
        compare(base, replace(base, repo="someone/else"))


# ----------------------------------------------------------------------------------
# Memory is per repository
# ----------------------------------------------------------------------------------


def test_one_repository_s_memory_never_answers_for_another(tmp_path: Path) -> None:
    store = Store(Database(tmp_path / "m.db"))
    root = _invoicer(tmp_path / "r")
    first = store.put_charter(_charter(root, "acme/invoicer", "a1"))
    other = store.put_charter(replace(_charter(root, "zeta/other", "b1"), summary="Other."))
    assert first.number == 1 and other.number == 1, "numbering is per repository"
    again = store.put_charter(_charter(root, "acme/invoicer", "a1"))
    assert again.number == 1, "the same commit is remembered once"

    assert store.charter("acme/invoicer").summary.startswith("Generate")  # type: ignore[union-attr]
    assert store.charter("zeta/other").summary == "Other."  # type: ignore[union-attr]
    assert store.charter("nobody/here") is None
    assert store.charter("acme/invoicer", "b1") is None, "another repo's commit leaked in"


def test_a_finding_is_judged_only_by_its_own_repository(tmp_path: Path) -> None:
    charter = _charter(_invoicer(tmp_path / "r"))
    finding = Observation(id="o", run_id="r", kind="api_change", symbol="x",
                          what_changed="w", how_we_know="h")
    with pytest.raises(ValueError, match="judged only by its own memory"):
        classify(finding, charter, repo="someone/else")


def test_relevance_follows_the_charter_and_orders_the_findings(tmp_path: Path) -> None:
    charter = _charter(_invoicer(tmp_path / "r"))

    def obs(kind: str, symbol: str, ref: str, what: str = "changed") -> Observation:
        return Observation(id=f"{kind}{symbol}", run_id="r", kind=kind, symbol=symbol,
                           what_changed=what, how_we_know="h", evidence_ref=ref)

    labelled = {
        o.symbol: o for o in grounded([
            obs("behavior_change", "test_vat", "tests/test_v.py::test_vat"),
            obs("api_change", "invoicer.validate.validate_vat", "invoicer/validate.py:7",
                "public symbol invoicer.validate.validate_vat was removed"),
            obs("lint_regression", "invoicer/validate.py", "invoicer/validate.py:F401"),
            obs("lint_regression", "tests/test_v.py", "tests/test_v.py:F401"),
        ], charter, repo="acme/invoicer")
    }
    assert labelled["test_vat"].relevance == "core"
    assert labelled["invoicer.validate.validate_vat"].relevance == "core"
    note = labelled["invoicer.validate.validate_vat"].relevance_note
    assert "Validate invoice totals" in note
    assert "invoicer/validate.py:1" in note
    assert labelled["invoicer/validate.py"].relevance == "supporting"
    assert labelled["tests/test_v.py"].relevance == "peripheral"
    assert WEIGHT["core"] > WEIGHT["supporting"] > WEIGHT["peripheral"]


# ----------------------------------------------------------------------------------
# Watching the repository move, and saying so at the right volume
# ----------------------------------------------------------------------------------


class _Hook(http.server.BaseHTTPRequestHandler):
    received: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        length = int(self.headers.get("Content-Length", 0))
        _Hook.received.append(json.loads(self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def webhook() -> Iterator[str]:
    _Hook.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def _keeper(tmp_path: Path, root: Path, hook: str = "") -> tuple[CharterKeeper, Store]:
    config = Config(repos=(RepoConfig(slug="acme/invoicer", clone_url=str(root)),))
    database = Database(tmp_path / "k.db")
    store = Store(database)
    bus = EventBus(database)
    store.put_repo(Repo(slug="acme/invoicer", added_at=time.time()))
    notifier = Notifier(config, store, bus, webhook_url=hook)
    return CharterKeeper(config, store, bus, notifier), store


def test_a_major_update_is_announced_once_and_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, webhook: str
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    root = _invoicer(tmp_path / "r")

    async def scenario() -> None:
        keeper, store = _keeper(tmp_path, root, webhook)
        keeper._bus.bind_loop(asyncio.get_running_loop())  # noqa: SLF001
        baseline = await keeper.check("acme/invoicer")
        assert baseline is not None and baseline.drift is None
        assert store.notifications() == [], "a first reading is not news"
        assert await keeper.check("acme/invoicer") is None, "nothing moved, nothing rebuilt"

        # The repository pivots: new purpose, a command removed, a major version.
        text = (root / "pyproject.toml").read_text()
        text = text.replace('version = "1.4.0"', 'version = "2.0.0"')
        text = text.replace("Generate and validate invoices for small businesses.",
                            "A hosted payments gateway with subscription billing.")
        text = text.replace('[project.scripts]\ninvoicer = "invoicer.cli:main"\n', "")
        (root / "pyproject.toml").write_text(text)
        (root / "README.md").write_text(_README.replace(
            "Generate and validate invoices for small businesses, entirely offline.",
            "Take card payments and run subscription billing as a hosted service.",
        ))
        _git(root, "commit", "-qam", "pivot")

        moved = await keeper.check("acme/invoicer")
        assert moved is not None and moved.drift is not None
        assert moved.drift.level == "major"
        assert moved.charter.number == 2

        notices = store.notifications(open_only=True, min_level="major")
        assert len(notices) == 1
        assert notices[0].kind == "repo.update"
        assert "purpose" in notices[0].title, "the headline should name what matters most"

        # Seen again — a restart, a forced rebuild — it is not announced twice.
        await keeper.refresh("acme/invoicer", moved.charter.sha, force=True)
        assert len(store.notifications()) == 1

        assert store.acknowledge(notices[0].id)
        assert store.notifications(open_only=True, min_level="major") == []

    asyncio.run(scenario())
    assert len(_Hook.received) == 1, "the webhook should fire exactly once"
    assert _Hook.received[0]["text"].startswith("[MAJOR] acme/invoicer:")
    assert _Hook.received[0]["prflagger"]["level"] == "major"


def test_quieter_updates_are_quieter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, webhook: str
) -> None:
    """Minor is history only; notable is a feed entry that interrupts nobody."""
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    root = _invoicer(tmp_path / "r")

    async def scenario() -> tuple[str, str, int, int]:
        keeper, store = _keeper(tmp_path, root, webhook)
        keeper._bus.bind_loop(asyncio.get_running_loop())  # noqa: SLF001
        await keeper.check("acme/invoicer")

        (root / "pyproject.toml").write_text(
            (root / "pyproject.toml").read_text().replace('"pydantic"]', '"pydantic", "httpx"]')
        )
        _git(root, "commit", "-qam", "add a dependency")
        minor = await keeper.check("acme/invoicer")

        (root / "invoicer" / "render.py").write_text(
            (root / "invoicer" / "render.py").read_text()
            + "\ndef render_html(i):\n    return i\n\ndef render_text(i):\n    return i\n"
        )
        _git(root, "commit", "-qam", "more renderers")
        notable = await keeper.check("acme/invoicer")
        assert minor is not None and minor.drift is not None
        assert notable is not None and notable.drift is not None
        return (
            minor.drift.level, notable.drift.level,
            len(store.notifications(min_level="notable")),
            len(store.notifications(open_only=True, min_level="major")),
        )

    minor, notable, feed, banners = asyncio.run(scenario())
    assert minor == "minor"
    assert notable == "notable"
    assert feed == 1, "only the notable change becomes a notification"
    assert banners == 0, "neither should reach the on-every-page banner"
    assert _Hook.received == [], "nothing below major goes to the webhook by default"


def test_a_failing_webhook_is_recorded_not_raised(tmp_path: Path) -> None:
    config = Config()
    database = Database(tmp_path / "n.db")
    store = Store(database)
    notifier = Notifier(config, store, EventBus(database),
                        webhook_url="http://127.0.0.1:9/unreachable")
    notice = notifier.raise_(repo="a/b", kind="repo.update", level="major",
                             title="t", body="b", sha="abc")
    assert notice is not None
    delivery = database.scalar("SELECT delivery FROM notifications WHERE id = ?", (notice.id,))
    assert json.loads(delivery)["ok"] is False


# ----------------------------------------------------------------------------------
# Reading history from the clone the service actually keeps
# ----------------------------------------------------------------------------------


def test_history_is_read_without_downloading_old_file_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service keeps blob-less clones; churn must come from trees alone.

    Counting lines per commit needs every old version of every file, which a
    partial clone fetches one commit at a time. On pallets/click (495 commits a
    year) that took the atlas 131 s against a 180 s limit; a busier repository
    exceeded it and its charter was never built.
    """
    from prflagger.atlas.cartography import churn_by_path
    from prflagger.vcs.worktrees import worktree_for

    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))
    origin = _invoicer(tmp_path / "origin")
    _git(origin, "config", "uploadpack.allowFilter", "true")
    for author, text in (("Ada", "one"), ("Grace", "two"), ("Ada", "three")):
        (origin / "invoicer" / "render.py").write_text(f"STAGE = {text!r}\n")
        _git(origin, "add", "-A")
        _git(origin, "-c", f"user.name={author}", "commit", "-q", "-m", text)
    head = _git(origin, "rev-parse", "HEAD")

    tree = worktree_for("acme/invoicer", head, url=f"file://{origin}")
    bare = tmp_path / "cache" / "repos" / "acme__invoicer.git"
    assert _git(bare, "config", "remote.origin.promisor") == "true", (
        "the clone is not partial, so this test would prove nothing"
    )

    # Nothing may be fetched from here on: an old blob would have to come from the
    # remote, and on GitHub that is one round trip per commit.
    origin.rename(tmp_path / "unreachable")

    churn = churn_by_path(tree)
    assert churn, "churn needed file contents the partial clone does not hold"
    assert churn["invoicer/render.py"].commits == 4  # the base commit and three more
    assert churn["invoicer/render.py"].authors == {"T", "Ada", "Grace"}


# ----------------------------------------------------------------------------------
# Upgrading an existing installation
# ----------------------------------------------------------------------------------


def test_an_older_database_gains_the_new_columns_in_place(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
        INSERT INTO schema_version VALUES (1, 0);
        CREATE TABLE observations (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT '', what_changed TEXT NOT NULL,
            how_we_know TEXT NOT NULL, evidence_ref TEXT NOT NULL DEFAULT '',
            severity REAL NOT NULL DEFAULT 0.5, confidence REAL NOT NULL DEFAULT 0.5,
            rank_score REAL NOT NULL DEFAULT 0, norm_id TEXT);
        INSERT INTO observations (id, run_id, kind, what_changed, how_we_know)
            VALUES ('kept', 'r', 'api_change', 'w', 'h');
    """)
    raw.commit()
    raw.close()

    database = Database(path)
    columns = {row["name"] for row in database.query("PRAGMA table_info(observations)")}
    assert {"relevance", "relevance_note"} <= columns
    assert database.scalar("SELECT id FROM observations") == "kept"
    Database(path)  # a second open must be a no-op, not an error
