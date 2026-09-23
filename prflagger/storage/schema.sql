-- PR Flagger v2 schema. Applied by storage.db.migrate() in numbered order.
--
-- Two rules shape this file:
--   * `events` is append-only and is the single source of truth for what a run
--     did. Every other table is a projection that can be rebuilt from it.
--   * Nothing an LLM returns picks a column. Extraction fills this known shape.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    slug            TEXT PRIMARY KEY,
    default_branch  TEXT NOT NULL DEFAULT 'main',
    toolchain_id    TEXT NOT NULL DEFAULT '',
    package_roots   TEXT NOT NULL DEFAULT '[]',   -- json array
    added_at        REAL NOT NULL,
    atlas_sha       TEXT NOT NULL DEFAULT '',
    atlas_built_at  REAL,
    brain_built_at  REAL,
    last_polled_at  REAL,
    poll_etag       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pulls (
    repo          TEXT NOT NULL,
    number        INTEGER NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL DEFAULT '',
    base_sha      TEXT NOT NULL DEFAULT '',
    head_sha      TEXT NOT NULL DEFAULT '',
    state         TEXT NOT NULL DEFAULT 'open',
    updated_at    TEXT NOT NULL DEFAULT '',
    additions     INTEGER NOT NULL DEFAULT 0,
    deletions     INTEGER NOT NULL DEFAULT 0,
    changed_files INTEGER NOT NULL DEFAULT 0,
    draft         INTEGER NOT NULL DEFAULT 0,
    seen_at       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, number),
    FOREIGN KEY (repo) REFERENCES repos(slug) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS pulls_state ON pulls (repo, state);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    repo         TEXT NOT NULL,
    pr_number    INTEGER NOT NULL DEFAULT 0,
    base_sha     TEXT NOT NULL DEFAULT '',
    head_sha     TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'queued',
    trigger      TEXT NOT NULL DEFAULT 'manual',
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    error        TEXT NOT NULL DEFAULT '',
    usd_spent    REAL NOT NULL DEFAULT 0,
    superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS runs_repo_pr ON runs (repo, pr_number, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_state   ON runs (state);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    stage           TEXT NOT NULL,
    image_tag       TEXT NOT NULL DEFAULT '',
    argv            TEXT NOT NULL DEFAULT '[]',   -- json array, shown verbatim in the UI
    outcome         TEXT,
    exit_code       INTEGER,
    duration_s      REAL,
    peak_rss_mb     INTEGER,
    memory_mb       INTEGER,
    timeout_s       INTEGER,
    log_path        TEXT NOT NULL DEFAULT '',
    started_at      REAL,
    finished_at     REAL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs (run_id);

CREATE TABLE IF NOT EXISTS observations (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    symbol       TEXT NOT NULL DEFAULT '',
    what_changed TEXT NOT NULL,
    how_we_know  TEXT NOT NULL,
    evidence_ref TEXT NOT NULL DEFAULT '',
    severity     REAL NOT NULL DEFAULT 0.5,
    confidence   REAL NOT NULL DEFAULT 0.5,
    rank_score   REAL NOT NULL DEFAULT 0,
    norm_id      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS observations_run ON observations (run_id, rank_score DESC);

CREATE TABLE IF NOT EXISTS adjudications (
    observation_id TEXT PRIMARY KEY,
    assessment     TEXT NOT NULL,
    reasoning      TEXT NOT NULL,
    citations      TEXT NOT NULL DEFAULT '[]',   -- json array; never empty in practice
    model          TEXT NOT NULL DEFAULT '',
    usd            REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS suggestions (
    observation_id TEXT PRIMARY KEY,
    summary        TEXT NOT NULL,
    rationale      TEXT NOT NULL DEFAULT '',
    patch_sketch   TEXT NOT NULL DEFAULT '',
    confidence     REAL NOT NULL DEFAULT 0.5,
    citations      TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS symbols (
    repo       TEXT NOT NULL,
    fqn        TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'function',
    file       TEXT NOT NULL DEFAULT '',
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end   INTEGER NOT NULL DEFAULT 0,
    lang       TEXT NOT NULL DEFAULT 'python',
    PRIMARY KEY (repo, fqn)
);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols (repo, file);

CREATE TABLE IF NOT EXISTS call_edges (
    repo   TEXT NOT NULL,
    caller TEXT NOT NULL,
    callee TEXT NOT NULL,
    PRIMARY KEY (repo, caller, callee)
);
CREATE INDEX IF NOT EXISTS call_edges_callee ON call_edges (repo, callee);

CREATE TABLE IF NOT EXISTS norms (
    id                 TEXT NOT NULL,
    repo               TEXT NOT NULL,
    statement          TEXT NOT NULL,
    scope              TEXT NOT NULL DEFAULT 'repo',
    support            INTEGER NOT NULL DEFAULT 0,
    distinct_reviewers INTEGER NOT NULL DEFAULT 0,
    confidence         REAL NOT NULL DEFAULT 0,
    evidence_prs       TEXT NOT NULL DEFAULT '[]',
    embedding          BLOB,
    PRIMARY KEY (repo, id)
);

CREATE TABLE IF NOT EXISTS atlases (
    repo       TEXT NOT NULL,
    sha        TEXT NOT NULL,
    built_at   REAL NOT NULL,
    payload    TEXT NOT NULL,                -- json; the whole Atlas, ready to serve
    PRIMARY KEY (repo, sha)
);

CREATE TABLE IF NOT EXISTS llm_spend (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 REAL NOT NULL,
    run_id             TEXT,
    repo               TEXT,
    model              TEXT NOT NULL,
    stage              TEXT NOT NULL DEFAULT '',
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    usd                REAL NOT NULL DEFAULT 0,
    cached             INTEGER NOT NULL DEFAULT 0   -- 1 = served from the disk cache, $0
);
CREATE INDEX IF NOT EXISTS llm_spend_run  ON llm_spend (run_id);
CREATE INDEX IF NOT EXISTS llm_spend_repo ON llm_spend (repo, ts);

-- Append-only. The live UI is a tail of this table; a late-joining client
-- replays from its cursor and then goes live, so what it sees is identical
-- either way.
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    run_id  TEXT,
    repo    TEXT,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq);
CREATE INDEX IF NOT EXISTS events_ts  ON events (ts);
