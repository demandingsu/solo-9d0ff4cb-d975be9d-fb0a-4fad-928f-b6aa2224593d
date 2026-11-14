"""FastAPI service: collect weighing records from instrument CSV files
and query the collected measurements.

Endpoints:
  POST /collect       {"directory": "/data/incoming"}  -> ingest new complete lines
  GET  /measurements  ?sample_id=&since=&until=&limit=&offset=
  GET  /files         tracked file identities and read positions
  GET  /rejected      complete lines that failed parsing (audit)
  GET  /health
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

import db as dbmod
from collector import collect_directory

DB_PATH = os.environ.get("DB_PATH", "/data/db/measurements.db")
DEFAULT_DIR = os.environ.get("INCOMING_DIR", "/data/incoming")

# In-process guard so concurrent /collect calls in the same worker
# serialize instead of piling up on the SQLite lock. Cross-process
# safety comes from BEGIN IMMEDIATE + busy_timeout.
_collect_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbmod.init_db(DB_PATH)
    yield


app = FastAPI(title="Weighing Line Collector", version="1.0.0", lifespan=lifespan)


class CollectRequest(BaseModel):
    directory: Optional[str] = None  # defaults to the mounted INCOMING_DIR


@app.post("/collect")
def collect(req: CollectRequest | None = None):
    directory = (req.directory if req and req.directory else DEFAULT_DIR)
    if not os.path.isdir(directory):
        raise HTTPException(status_code=400, detail=f"not a directory: {directory}")
    with _collect_lock:
        conn = dbmod.connect(DB_PATH)
        try:
            report = collect_directory(conn, directory)
        finally:
            conn.close()
    return {
        "directory": report.directory,
        "files_seen": report.files_seen,
        "measurements_inserted": report.measurements,
        "rejected_lines": report.rejected,
        "files": [
            {
                "path": f.path,
                "dev": f.dev,
                "ino": f.ino,
                "generation": f.generation,
                "start_offset": f.start_offset,
                "end_offset": f.end_offset,
                "lines_read": f.lines_read,
                "measurements": f.measurements,
                "rejected": f.rejected,
                **({"note": f.note} if f.note else {}),
            }
            for f in report.files
        ],
    }


@app.get("/measurements")
def measurements(
    sample_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="weighed_at >= since"),
    until: Optional[str] = Query(default=None, description="weighed_at <= until"),
    limit: int = Query(default=100, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
):
    sql = ("SELECT m.id, m.sample_id, m.weighed_at, m.grams, m.raw,"
           "       m.collected_at, f.path AS source_file, m.line_no"
           " FROM measurements m JOIN files f ON f.id = m.file_id")
    cond, params = [], []
    if sample_id:
        cond.append("m.sample_id = ?")
        params.append(sample_id)
    if since:
        cond.append("m.weighed_at >= ?")
        params.append(since)
    if until:
        cond.append("m.weighed_at <= ?")
        params.append(until)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    count_sql = "SELECT COUNT(*) AS n FROM (" + sql + ")"
    sql += " ORDER BY m.id LIMIT ? OFFSET ?"
    params_page = params + [limit, offset]

    conn = dbmod.connect(DB_PATH)
    try:
        total = conn.execute(count_sql, params).fetchone()["n"]
        rows = conn.execute(sql, params_page).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


@app.get("/files")
def files():
    conn = dbmod.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, dev, ino, generation, path, offset, lines, updated_at"
            " FROM files ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/rejected")
def rejected(limit: int = Query(default=100, ge=1, le=10000)):
    conn = dbmod.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT r.id, r.line_no, r.raw, r.reason, r.collected_at, f.path"
            " FROM rejected_lines r JOIN files f ON f.id = r.file_id"
            " ORDER BY r.id LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/health")
def health():
    return {"status": "ok", "db": DB_PATH, "incoming_dir": DEFAULT_DIR}
