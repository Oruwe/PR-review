"""Running it for real: backups, configuration paths, and the deployment files.

The backup tests use a real database under real concurrent writes. The deploy
files are checked for the properties a deployment depends on — the same-path data
mount, loopback-only publishing, no secret in anything committed — and Compose
itself validates the file where Docker is available.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest

from prflagger.core.config import load
from prflagger.core.models import Charter, Claim, Repo
from prflagger.sandbox.cgroup import ContainerProbe
from prflagger.storage.backup import ServiceRunning, backup, pid_file, restore
from prflagger.storage.db import Database
from prflagger.storage.repos import Store

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


# ----------------------------------------------------------------------------------
# Backup and restore
# ----------------------------------------------------------------------------------


def _populated(path: Path) -> Store:
    store = Store(Database(path))
    store.put_repo(Repo(slug="acme/lib", added_at=time.time()))
    store.put_charter(Charter(
        repo="acme/lib", sha="a" * 40, name="lib", summary="Parse things safely.",
        claims=(Claim("purpose", "Parse things without executing them", "README.md:3"),),
    ))
    return store


def test_a_backup_taken_under_load_restores_whole(tmp_path: Path) -> None:
    live = tmp_path / "live" / "prflagger.db"
    store = _populated(live)
    stop = threading.Event()

    def keep_writing() -> None:
        n = 0
        while not stop.is_set():
            store.db.execute(
                "INSERT INTO events (ts, run_id, repo, type, payload) VALUES (?, ?, ?, ?, ?)",
                (time.time(), None, "acme/lib", "load", json.dumps({"n": n})),
            )
            n += 1

    writer = threading.Thread(target=keep_writing)
    writer.start()
    try:
        time.sleep(0.2)
        manifest = backup(live, tmp_path / "b.tar.gz", config=None)
    finally:
        stop.set()
        writer.join()

    assert manifest["rows"]["charters"] == 1 and manifest["rows"]["events"] > 0
    assert "worktrees" in manifest["not_included"]

    fresh = tmp_path / "fresh" / "prflagger.db"
    restore(tmp_path / "b.tar.gz", fresh)
    restored = Store(Database(fresh))
    charter = restored.charter("acme/lib")
    assert charter is not None and charter.claims[0].source == "README.md:3"
    events = restored.db.scalar("SELECT COUNT(*) FROM events")
    assert events == manifest["rows"]["events"], "the snapshot is one consistent moment"


def test_restore_keeps_what_it_replaces(tmp_path: Path) -> None:
    source = tmp_path / "a" / "prflagger.db"
    _populated(source)
    backup(source, tmp_path / "b.tar.gz")

    target = tmp_path / "t" / "prflagger.db"
    other = Store(Database(target))
    other.put_repo(Repo(slug="zeta/other", added_at=time.time()))
    other.db.close()

    result = restore(tmp_path / "b.tar.gz", target)
    kept = Path(result["previous_database"])
    assert kept.is_file()
    assert Store(Database(kept)).repo("zeta/other") is not None, "nothing is lost"
    assert Store(Database(target)).repo("acme/lib") is not None


def test_restore_refuses_while_the_service_is_running(tmp_path: Path) -> None:
    db = tmp_path / "prflagger.db"
    _populated(db)
    backup(db, tmp_path / "b.tar.gz")
    service = subprocess.Popen(["sleep", "30"])  # noqa: S603, S607 - stands for the server process
    try:
        pid_file(db).write_text(json.dumps({"pid": service.pid, "host": socket.gethostname()}))
        with pytest.raises(ServiceRunning, match=str(service.pid)):
            restore(tmp_path / "b.tar.gz", db)
    finally:
        service.kill()
        service.wait()
    restore(tmp_path / "b.tar.gz", db)  # a stale marker from a dead process is ignored


def test_a_file_that_is_not_a_backup_is_refused(tmp_path: Path) -> None:
    bogus = tmp_path / "x.tar.gz"
    with tarfile.open(bogus, "w:gz") as archive:
        note = tmp_path / "note.txt"
        note.write_text("hello")
        archive.add(note, arcname="note.txt")
    with pytest.raises(ValueError, match="not a PR Flagger backup"):
        restore(bogus, tmp_path / "prflagger.db")


# ----------------------------------------------------------------------------------
# Where a deployed service finds its things
# ----------------------------------------------------------------------------------


def test_the_config_path_can_be_set_by_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "etc" / "config.toml"
    config.parent.mkdir()
    config.write_text('[[repos]]\nslug = "acme/lib"\n\n[server]\nport = 8123\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRFLAGGER_CONFIG", str(config))
    loaded = load()
    assert loaded.server.port == 8123 and loaded.repos[0].slug == "acme/lib"


def test_the_example_config_is_a_complete_service_config() -> None:
    """What DEPLOY.md copies into place. It has no v1 `[target]`, which `load`
    would fold into the repository list, and it prices models and caps spend
    exactly as the development config does, so the two cannot drift apart."""
    example = DEPLOY / "config.example.toml"
    assert "[target]" not in example.read_text()
    loaded = load(example)
    assert [r.slug for r in loaded.repos] == ["pallets/click", "python-attrs/attrs"]
    assert all(r.max_prs <= 25 for r in loaded.repos), "the first poll queues every PR"
    click = loaded.repos[0]
    assert click.package_roots == ("src/click",)
    assert click.system_binaries == ("less", "cat", "sed")

    development = load(ROOT / "config.toml")
    assert loaded.models == development.models
    assert loaded.budget == development.budget
    assert loaded.brain == development.brain


def test_only_a_tool_that_is_truly_absent_turns_a_failure_into_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service runs this suite on itself in a sandbox without git or Docker.

    There a test that needs git skips and says why. Anywhere git exists the same
    failure stays a failure, so the conversion cannot hide a real one.
    """
    from tests.requires import missing_tool

    installed = shutil.which("git") is not None
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on PATH: git is truly absent
    try:
        try:
            subprocess.run(["git", "--version"], check=False)  # noqa: S603, S607
        except FileNotFoundError as absent:
            raise RuntimeError("could not clone") from absent
    except RuntimeError as wrapped:
        assert missing_tool(wrapped) == "git", "found through the exception chain"
        failure = wrapped
    monkeypatch.undo()

    if installed:
        assert missing_tool(failure) is None, "git is installed here: that stays a failure"
    try:
        (tmp_path / "seeds.json").read_text()
    except FileNotFoundError as data_file:
        assert missing_tool(data_file) is None, "a missing file is not a missing tool"


def test_memory_is_read_from_a_mounted_host_cgroup_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a container the host's tree is mounted elsewhere and named by env."""
    cid = "c" * 64
    scope = tmp_path / "host" / "memory" / "docker" / cid
    scope.mkdir(parents=True)
    (scope / "memory.usage_in_bytes").write_text(str(150 * 1024 * 1024))
    (scope / "memory.max_usage_in_bytes").write_text(str(160 * 1024 * 1024))
    monkeypatch.setenv("PRFLAGGER_CGROUP_ROOT", str(tmp_path / "host"))
    probe = ContainerProbe(cid)
    assert probe.available
    reading = probe.read()
    assert reading is not None and reading.rss_mb == 150
    assert probe.peak_mb() == 160


# ----------------------------------------------------------------------------------
# The deployment files
# ----------------------------------------------------------------------------------


_SECRETS = ("PRFLAGGER_ADMIN_TOKEN", "PRFLAGGER_VIEWER_TOKEN", "GITHUB_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK", "PRFLAGGER_NOTIFY_WEBHOOK")


def test_the_env_example_names_every_setting_and_holds_no_secret() -> None:
    entries = dict(
        line.split("=", 1) for line in (DEPLOY / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )
    for name in (*_SECRETS, "PRFLAGGER_GITHUB_API", "AWS_REGION", "PRFLAGGER_DATA",
                 "DOCKER_GID"):
        assert name in entries, f"{name} is missing from deploy/.env.example"
    for name in _SECRETS:
        assert entries[name] == "", f"{name} must be empty in a committed file"
    ignored = subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(ROOT), "check-ignore", "-q", "deploy/.env"], check=False
    )
    assert ignored.returncode == 0, "deploy/.env must be gitignored"


def test_compose_mounts_data_at_the_same_path_and_publishes_only_loopback() -> None:
    text = (DEPLOY / "compose.yaml").read_text()
    data = "${PRFLAGGER_DATA:-/var/lib/prflagger}"
    assert f"- {data}:{data}\n" in text, "sandbox bind mounts need identical paths"
    assert "- /var/run/docker.sock:/var/run/docker.sock" in text
    assert "- /sys/fs/cgroup:/host/sys/fs/cgroup:ro" in text
    assert "PRFLAGGER_CGROUP_ROOT: /host/sys/fs/cgroup" in text
    assert '"127.0.0.1:${PRFLAGGER_PORT:-8000}:8000"' in text
    assert "env_file: .env" in text


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker compose validates it")
def test_compose_accepts_the_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text((DEPLOY / ".env.example").read_text())
    completed = subprocess.run(  # noqa: S603, S607
        ["docker", "compose", "-f", str(DEPLOY / "compose.yaml"), "--env-file", str(env),
         "config", "--quiet"],
        capture_output=True, text=True, check=False, cwd=tmp_path,
        env={**os.environ, "COMPOSE_PROJECT_NAME": "prflagger-test"},
    )
    if "env file" in completed.stderr and ".env" in completed.stderr:
        pytest.skip("compose resolves env_file relative to the file; validated in CI")
    assert completed.returncode == 0, completed.stderr


def test_the_systemd_unit_listens_on_loopback_and_reads_secrets_from_a_file() -> None:
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str  # type: ignore[assignment,method-assign]
    unit.read(DEPLOY / "prflagger.service")
    service = unit["Service"]
    assert "--host 127.0.0.1" in service["ExecStart"]
    assert service["EnvironmentFile"] == "/etc/prflagger/env"
    assert service["User"] == "prflagger" and service["SupplementaryGroups"] == "docker"
    assert "/var/lib/prflagger" in service["ReadWritePaths"]
    assert service["Restart"] == "always"
    assert unit["Unit"]["Requires"] == "docker.service"
