"""SQLite access: connection, migration, and the small helpers everything uses.

One database file, WAL mode, `check_same_thread=False` because FastAPI serves
from a thread pool while the engine runs on the loop. Writes are serialised
through a single lock — SQLite allows one writer, and pretending otherwise
produces `database is locked` under exactly the concurrency this service has.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from prflagger.core.config import cache_root

__all__ = [
    "SCHEMA_VERSION",
    "Database",
    "connect",
    "default_path",
    "json_col",
]

log = structlog.get_logger(__name__)

#: Bumped when `schema.sql` changes in a way an existing database needs applied.
SCHEMA_VERSION = 2

#: Columns added after a table first shipped. `CREATE TABLE IF NOT EXISTS` cannot
#: add a column to a table that already exists, so a database created by an
#: earlier version would otherwise keep the old shape and fail on the first query
#: that names the new column. Each entry is applied only if the column is absent,
#: which keeps migration idempotent: running it twice changes nothing.
_ADDED_COLUMNS: tuple[tuple[int, str, str, str], ...] = (
    (2, "repos", "charter_sha", "TEXT NOT NULL DEFAULT ''"),
    (2, "repos", "branch_head", "TEXT NOT NULL DEFAULT ''"),
    (2, "runs", "charter_impact", "TEXT NOT NULL DEFAULT ''"),
    (2, "observations", "relevance", "TEXT NOT NULL DEFAULT ''"),
    (2, "observations", "relevance_note", "TEXT NOT NULL DEFAULT ''"),
)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def default_path() -> Path:
    return cache_root() / "prflagger.db"


def json_col(value: Any) -> str:
    """Canonical JSON for a text column. Sorted keys keep rows comparable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_default)


def _default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    if hasattr(value, "value"):  # Enum
        return value.value
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class Database:
    """The one connection, its lock, and the migration runner.

    Read queries go straight through; writes take the lock. That is the whole
    concurrency model, and it is sufficient because the only writer that matters
    is the engine, which is single-threaded on the asyncio loop.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self.migrate()

    # -- schema ---------------------------------------------------------------

    def migrate(self) -> int:
        """Apply the schema if it is not already at `SCHEMA_VERSION`.

        `schema.sql` is written entirely with `IF NOT EXISTS`, so applying it to
        an existing database is a no-op. That keeps migration a single idempotent
        step rather than a chain that can half-apply.
        """
        with self._lock:
            self._connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            for _, table, column, declaration in _ADDED_COLUMNS:
                existing = {
                    row["name"]
                    for row in self._connection.execute(f"PRAGMA table_info({table})")
                }
                if column not in existing:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )
                    log.info("db.column_added", table=table, column=column)
            current = self._connection.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()["v"]
            if current != SCHEMA_VERSION:
                self._connection.execute(
                    "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, time.time()),
                )
                log.info("db.migrated", path=str(self.path), version=SCHEMA_VERSION)
        return SCHEMA_VERSION

    # -- reads ----------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, tuple(params)))

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._connection.execute(sql, tuple(params)).fetchone()
        return row

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # -- writes ---------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._lock:
            self._connection.executemany(sql, [tuple(r) for r in rows])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A real transaction. Rolls back on any exception."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()


_DEFAULT: Database | None = None
_DEFAULT_LOCK = threading.Lock()


def connect(path: Path | str | None = None) -> Database:
    """The process-wide database. Passing an explicit path makes a separate one."""
    global _DEFAULT
    if path is not None:
        return Database(path)
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Database()
    return _DEFAULT
