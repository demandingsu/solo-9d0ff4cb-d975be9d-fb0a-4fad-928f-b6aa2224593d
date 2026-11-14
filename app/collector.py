"""Core collection logic.

Guarantees:
  * Identity by (dev, ino): renaming a file (rotation) never re-collects it.
  * Only bytes up to the last newline are consumed; a trailing partial line
    is left for the next run, when the instrument finishes writing it.
  * Measurement rows and the new read position are written in ONE SQLite
    transaction (BEGIN IMMEDIATE), so a crash/restart or a concurrent
    /collect call can never produce duplicates or gaps.
  * If a file shrinks below the stored offset (truncated / inode reused),
    a new generation starts at offset 0; old rows stay untouched.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from db import utcnow

CSV_SUFFIXES = (".csv",)


@dataclass
class FileReport:
    path: str
    dev: int
    ino: int
    generation: int
    start_offset: int
    end_offset: int
    lines_read: int = 0
    measurements: int = 0
    rejected: int = 0
    note: str = ""


@dataclass
class CollectReport:
    directory: str
    files_seen: int = 0
    measurements: int = 0
    rejected: int = 0
    files: list = field(default_factory=list)


def parse_line(raw: str):
    """Parse 'sample_id,weighed_at,grams'. Returns (sample_id, weighed_at, grams)
    or raises ValueError."""
    parts = raw.split(",")
    if len(parts) != 3:
        raise ValueError(f"expected 3 comma-separated fields, got {len(parts)}")
    sample_id, weighed_at, grams_s = (p.strip() for p in parts)
    if not sample_id:
        raise ValueError("empty sample id")
    if not weighed_at:
        raise ValueError("empty weighing timestamp")
    try:
        grams = float(grams_s)
    except ValueError:
        raise ValueError(f"invalid grams value: {grams_s!r}")
    return sample_id, weighed_at, grams


def _get_or_create_file(conn, dev: int, ino: int, path: str, size: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM files WHERE dev=? AND ino=?", (dev, ino)
    ).fetchone()
    now = utcnow()
    if row is None:
        conn.execute(
            "INSERT INTO files (dev, ino, generation, path, offset, lines, updated_at)"
            " VALUES (?,?,0,?,0,0,?)",
            (dev, ino, path, now),
        )
        row = conn.execute(
            "SELECT * FROM files WHERE dev=? AND ino=?", (dev, ino)
        ).fetchone()
    elif size < row["offset"]:
        # File shrank: truncated or inode reused by a new file. Start a new
        # generation at offset 0; previous measurements remain for audit.
        conn.execute(
            "UPDATE files SET generation=generation+1, offset=0, lines=0,"
            " path=?, updated_at=? WHERE id=?",
            (path, now, row["id"]),
        )
        row = conn.execute("SELECT * FROM files WHERE id=?", (row["id"],)).fetchone()
    elif row["path"] != path:
        # Same identity, new name (rotation rename): just record the path.
        conn.execute(
            "UPDATE files SET path=?, updated_at=? WHERE id=?",
            (path, now, row["id"]),
        )
        row = conn.execute("SELECT * FROM files WHERE id=?", (row["id"],)).fetchone()
    return row


def _collect_file(conn, directory: str, name: str, report: CollectReport) -> None:
    path = os.path.join(directory, name)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return  # rotated away between listdir and stat; next run will find it
    if not os.path.isfile(path):
        return

    report.files_seen += 1
    frow = _get_or_create_file(conn, st.st_dev, st.st_ino, path, st.st_size)
    frep = FileReport(
        path=path,
        dev=st.st_dev,
        ino=st.st_ino,
        generation=frow["generation"],
        start_offset=frow["offset"],
        end_offset=frow["offset"],
    )

    if st.st_size > frow["offset"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(frow["offset"])
                chunk = fh.read()
        except FileNotFoundError:
            return  # rotated away between stat and open; next run will find it

        # Only consume up to the last newline. A trailing partial line
        # (instrument still writing) stays unconsumed for the next run.
        # b'\n' can never be part of a multi-byte UTF-8 sequence, so the
        # cut point is always a valid character boundary.
        last_nl = chunk.rfind(b"\n")
        if last_nl >= 0:
            complete = chunk[: last_nl + 1]
            text = complete.decode("utf-8")
            raw_lines = text.split("\n")[:-1]  # trailing element after last \n is ''

            now = utcnow()
            line_no = frow["lines"]
            for raw in raw_lines:
                line_no += 1
                frep.lines_read += 1
                record = raw.rstrip("\r")
                if not record.strip():
                    continue  # blank line: counted for numbering, not stored
                try:
                    sample_id, weighed_at, grams = parse_line(record)
                except ValueError as exc:
                    conn.execute(
                        "INSERT OR IGNORE INTO rejected_lines"
                        " (file_id, generation, line_no, raw, reason, collected_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (frow["id"], frow["generation"], line_no, record, str(exc), now),
                    )
                    frep.rejected += 1
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO measurements"
                        " (file_id, generation, line_no, sample_id, weighed_at,"
                        "  grams, raw, collected_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (frow["id"], frow["generation"], line_no, sample_id,
                         weighed_at, grams, record, now),
                    )
                    frep.measurements += 1

            new_offset = frow["offset"] + len(complete)
            conn.execute(
                "UPDATE files SET offset=?, lines=?, updated_at=? WHERE id=?",
                (new_offset, frow["lines"] + frep.lines_read, utcnow(), frow["id"]),
            )
            frep.end_offset = new_offset
        else:
            frep.note = "partial line pending (no newline yet)"

    report.measurements += frep.measurements
    report.rejected += frep.rejected
    report.files.append(frep)


def collect_directory(conn: sqlite3.Connection, directory: str) -> CollectReport:
    """Scan `directory` once and ingest all new complete lines.

    The whole run is a single BEGIN IMMEDIATE transaction: concurrent runs
    serialize on the database write lock, and a crash leaves either the old
    or the new state, never anything in between.
    """
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        raise NotADirectoryError(f"not a directory: {directory}")

    report = CollectReport(directory=directory)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name in sorted(os.listdir(directory)):
            if name.lower().endswith(CSV_SUFFIXES):
                _collect_file(conn, directory, name, report)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report
