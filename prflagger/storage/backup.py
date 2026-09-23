"""Backing up and restoring what the service has learned.

Everything worth keeping is in one SQLite database: runs and their findings,
each repository's charter history, mined norms, review comments, notifications
and model spend. Worktrees, images, run transcripts and cached model answers are
all rebuilt on demand, so they are left out — the manifest says so.

The copy uses SQLite's online backup API, which produces a consistent snapshot
while the service keeps writing (WAL mode), so a backup never needs downtime.
A restore does: it refuses while the service is running on this machine, and
it keeps the database it replaces beside the restored one.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from prflagger.storage.db import SCHEMA_VERSION

__all__ = ["ServiceRunning", "backup", "pid_file", "restore", "service_running"]

_DB_MEMBER = "prflagger.db"
_CONFIG_MEMBER = "config.toml"
_MANIFEST = "MANIFEST.json"
_REBUILT = ["worktrees", "sandbox images", "run transcripts", "cached model answers",
            "bare clones"]


class ServiceRunning(RuntimeError):
    """A restore was refused because the service still has the database open."""


def pid_file(db_path: Path) -> Path:
    return db_path.with_name("service.pid")


def service_running(db_path: Path) -> int | None:
    """The pid of a service using `db_path` on this machine, or None."""
    try:
        record = json.loads(pid_file(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = int(record.get("pid", 0))
    if record.get("host") != socket.gethostname() or pid <= 0 or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # alive, owned by someone else
    return pid


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])  # noqa: S608 - names from sqlite_master
        for table in sorted(tables)
    }


def backup(db_path: Path, out: Path, *, config: Path | None = None) -> dict[str, Any]:
    """Write a consistent snapshot of the database (and config) to `out` (.tar.gz)."""
    if not db_path.is_file():
        raise FileNotFoundError(f"no database at {db_path}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        snapshot = Path(scratch) / _DB_MEMBER
        source = sqlite3.connect(db_path)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            counts = _counts(target)
        finally:
            target.close()
            source.close()
        manifest = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "schema_version": SCHEMA_VERSION,
            "rows": counts,
            "includes_config": bool(config and config.is_file()),
            "not_included": _REBUILT,
            "why_not": "rebuilt on demand from the repositories themselves",
        }
        partial = out.with_name(out.name + ".partial")
        with tarfile.open(partial, "w:gz") as archive:
            archive.add(snapshot, arcname=_DB_MEMBER)
            if config and config.is_file():
                archive.add(config, arcname=_CONFIG_MEMBER)
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(_MANIFEST)
            info.size, info.mtime = len(data), int(time.time())
            archive.addfile(info, io.BytesIO(data))
        partial.replace(out)  # a reader never sees half an archive
    return manifest


def restore(archive_path: Path, db_path: Path, *, config: Path | None = None
            ) -> dict[str, Any]:
    """Put a backup's database (and config, if given a path) back in place."""
    running = service_running(db_path)
    if running is not None:
        raise ServiceRunning(
            f"the service (pid {running}) is using {db_path}; stop it before restoring"
        )
    with tarfile.open(archive_path, "r:gz") as archive:
        names = set(archive.getnames())
        if _DB_MEMBER not in names or _MANIFEST not in names:
            raise ValueError(f"{archive_path} is not a PR Flagger backup")
        manifest = json.loads(archive.extractfile(_MANIFEST).read())  # type: ignore[union-attr]
        db_path.parent.mkdir(parents=True, exist_ok=True)
        staged = db_path.with_name(db_path.name + ".restoring")
        staged.write_bytes(archive.extractfile(_DB_MEMBER).read())  # type: ignore[union-attr]
        check = sqlite3.connect(staged)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("the backup's database failed SQLite's integrity check")
        finally:
            check.close()

        kept = None
        if db_path.exists():
            kept = db_path.with_name(f"{db_path.name}.before-restore-{int(time.time())}")
            db_path.replace(kept)
        for suffix in ("-wal", "-shm"):
            # The replaced database's journal travels with it, so the kept copy
            # still opens with every write it had.
            journal = Path(str(db_path) + suffix)
            if journal.exists():
                if kept is not None:
                    journal.replace(Path(str(kept) + suffix))
                else:
                    journal.unlink()
        staged.replace(db_path)

        if config is not None and _CONFIG_MEMBER in names:
            config.write_bytes(archive.extractfile(_CONFIG_MEMBER).read())  # type: ignore[union-attr]
    return {**manifest, "previous_database": str(kept) if kept else None}
