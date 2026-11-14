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

-- Handover vouchers. A voucher is created by POST /vouchers; at acceptance
-- time (the first request for a request_id) the matching measurement rows
-- are frozen into voucher_records, so later collection ("补采") can never
-- alter an already delivered voucher.
CREATE TABLE IF NOT EXISTS vouchers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT    NOT NULL UNIQUE,          -- idempotency key supplied by the client
    sample_id       TEXT    NOT NULL,                 -- frozen request parameters
    since           TEXT    NOT NULL,
    until           TEXT    NOT NULL,
    status          TEXT    NOT NULL,                 -- PENDING | GENERATING | COMPLETED | FAILED
    file_path       TEXT,                             -- final CSV path (only when COMPLETED)
    failure_reason  TEXT,                             -- last failure detail (only when FAILED)
    record_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- Immutable snapshot of the measurements a voucher was accepted with.
-- source_path/generation/line_no are frozen values: even if the source
-- file is later renamed or truncated, the voucher keeps the original.
CREATE TABLE IF NOT EXISTS voucher_records (
    voucher_id      INTEGER NOT NULL REFERENCES vouchers(id),
    position        INTEGER NOT NULL,                 -- 1-based row order inside the CSV
    measurement_id  INTEGER NOT NULL,
    sample_id       TEXT    NOT NULL,                 -- original column 1
    weighed_at      TEXT    NOT NULL,                 -- original column 2
    grams           TEXT    NOT NULL,                 -- original column 3 (kept verbatim)
    source_path     TEXT    NOT NULL,
    generation      INTEGER NOT NULL,
    line_no         INTEGER NOT NULL,
    PRIMARY KEY (voucher_id, position)
);

CREATE INDEX IF NOT EXISTS idx_voucher_records_mid ON voucher_records(measurement_id);
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
