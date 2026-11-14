"""SQLite persistence layer for the weighing-line collector.

The database stores, in one transactional unit per collection run:
  * measurement rows parsed from instrument CSV files
  * the per-file read position (byte offset + line counter)

File identity is (st_dev, st_ino), so renaming a file during rotation
never changes its identity and cannot cause re-collection.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dev         INTEGER NOT NULL,
    ino         INTEGER NOT NULL,
    generation  INTEGER NOT NULL DEFAULT 0,   -- bumped when a truncated/replaced inode is detected
    path        TEXT    NOT NULL,             -- last path this identity was seen at (informational)
    offset      INTEGER NOT NULL DEFAULT 0,   -- bytes consumed so far (only complete lines)
    lines       INTEGER NOT NULL DEFAULT 0,   -- complete lines consumed so far
    updated_at  TEXT    NOT NULL,
    UNIQUE (dev, ino)
);

CREATE TABLE IF NOT EXISTS measurements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id      INTEGER NOT NULL REFERENCES files(id),
    generation   INTEGER NOT NULL,
    line_no      INTEGER NOT NULL,            -- 1-based line number within the file generation
    sample_id    TEXT    NOT NULL,
    weighed_at   TEXT    NOT NULL,
    grams        REAL    NOT NULL,
    raw          TEXT    NOT NULL,            -- original line, kept for audit
    collected_at TEXT    NOT NULL,
    UNIQUE (file_id, generation, line_no)     -- idempotency guard
);

CREATE TABLE IF NOT EXISTS rejected_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id      INTEGER NOT NULL REFERENCES files(id),
    generation   INTEGER NOT NULL,
    line_no      INTEGER NOT NULL,
    raw          TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    collected_at TEXT    NOT NULL,
    UNIQUE (file_id, generation, line_no)
);

CREATE INDEX IF NOT EXISTS idx_measurements_sample    ON measurements(sample_id);
CREATE INDEX IF NOT EXISTS idx_measurements_weighed_at ON measurements(weighed_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection. busy_timeout makes concurrent collectors wait
    instead of failing with 'database is locked'."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
