"""SQLite storage layer: schema, connections and tuning.

Tables:
  repos         one row per ingested repository
  authors       per-repo author identities (name, email) + manual merge targets
  commits       non-merge commits reachable from HEAD (the set H-bar)
  file_changes  one row per changed file per commit (binary / zero-churn skipped)
  dir_changes   pre-rolled-up rows per ancestor directory per commit
  objects       every path ever touched (kind: file|dir) - the sets H[F], H[D]

Secondary indexes are created after a bulk load (see indexer.create_indexes) so
that ingestion inserts are not slowed down by index maintenance.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_configured_dir: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('zip', 'url')),
    origin       TEXT,
    path         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    commit_count INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS authors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    merged_into INTEGER,
    UNIQUE (repo_id, name, email)
);

CREATE TABLE IF NOT EXISTS commits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    hash         TEXT NOT NULL,
    parent_hash  TEXT,
    author_id    INTEGER NOT NULL,
    committer_ts INTEGER NOT NULL,
    subject      TEXT,
    UNIQUE (repo_id, hash)
);

CREATE TABLE IF NOT EXISTS file_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL,
    commit_id    INTEGER NOT NULL,
    path         TEXT NOT NULL,
    adds         INTEGER NOT NULL,
    dels         INTEGER NOT NULL,
    author_id    INTEGER NOT NULL,
    committer_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dir_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id      INTEGER NOT NULL,
    commit_id    INTEGER NOT NULL,
    path         TEXT NOT NULL,
    adds         INTEGER NOT NULL,
    dels         INTEGER NOT NULL,
    author_id    INTEGER NOT NULL,
    committer_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS objects (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    path    TEXT NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('file', 'dir')),
    UNIQUE (repo_id, path, kind)
);
"""

# Covering indexes for the metric aggregations:
#   object metrics/:  WHERE repo_id, path  [+ committer_ts range] [+ author]
#   subtree scans:    path >= lo AND path < hi (prefix range on an index)
#   list-mode filters / reindex deletes: commit_id
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_file_obj    ON file_changes(repo_id, path, committer_ts, author_id, adds, dels);
CREATE INDEX IF NOT EXISTS idx_file_commit ON file_changes(repo_id, commit_id);
CREATE INDEX IF NOT EXISTS idx_dir_obj     ON dir_changes(repo_id, path, committer_ts, author_id, adds, dels);
CREATE INDEX IF NOT EXISTS idx_dir_commit  ON dir_changes(repo_id, commit_id);
CREATE INDEX IF NOT EXISTS idx_commits_ts  ON commits(repo_id, committer_ts);
CREATE INDEX IF NOT EXISTS idx_commits_auth ON commits(repo_id, author_id);
CREATE INDEX IF NOT EXISTS idx_objects_kind ON objects(repo_id, kind);
"""

INDEX_NAMES = [
    "idx_file_obj", "idx_file_commit", "idx_dir_obj", "idx_dir_commit",
    "idx_commits_ts", "idx_commits_auth", "idx_objects_kind",
]


def configure(data_dir: str | os.PathLike) -> Path:
    """Set the data directory (must be called before first connection)."""
    global _configured_dir
    _configured_dir = Path(data_dir)
    _configured_dir.mkdir(parents=True, exist_ok=True)
    return _configured_dir


def data_dir() -> Path:
    if _configured_dir is None:
        configure(os.environ.get("RAT_DATA_DIR", DEFAULT_DATA_DIR))
    assert _configured_dir is not None
    return _configured_dir


def db_path() -> Path:
    return data_dir() / "rat.sqlite3"


def get_conn() -> sqlite3.Connection:
    """Open a connection with sane pragmas. Use one per request/thread."""
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    """Create the data dir, switch to WAL and create tables (no secondary indexes)."""
    data_dir()
    conn = get_conn()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEXES)
    conn.execute("ANALYZE")


def drop_indexes(conn: sqlite3.Connection) -> None:
    for name in INDEX_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
