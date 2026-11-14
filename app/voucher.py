"""Handover voucher generation.

A voucher ("凭证") is the immutable set of weighing records handed from the
batching-line shift operator to the traceability team. Its semantics:

  * The client submits (request_id, sample_id, since, until). The first
    accepted request FREEZES the matching measurements and their sources
    into voucher_records in the same transaction that creates the voucher.
  * A CSV (measurement id, the original three columns, source file,
    generation, line number) is then built in a background worker, so
    collection and query endpoints stay available while it runs.
  * The same request_id with the same parameters always returns the same
    voucher; different parameters are a 409 conflict; concurrent first
    submissions are serialized by BEGIN IMMEDIATE, so exactly one voucher
    (and one CSV) is ever produced.
  * Records that arrive later ("补采") are never added to an existing
    voucher, and a retry / process restart keeps using the frozen record
    set and frozen source descriptions.
  * Only a COMPLETED voucher is downloadable. The CSV is written to a
    temp file, fsynced and atomically renamed; if the process dies between
    the rename and the status update, startup recovery adopts the finished
    file instead of rebuilding it. Partial temp files are cleaned up.

Only the Python standard library is used, so this module is unit-testable
without the web framework installed.
"""

from __future__ import annotations

# Allow `python -I voucher.py ...` (isolated mode) to find sibling modules.
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import csv
import io
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

from db import connect, utcnow

# CSV header (Chinese, matching the domain language). utf-8-sig adds a BOM
# so Excel opens the Chinese columns correctly.
CSV_HEADER = [
    "测量编号", "样品编号", "称量时间", "克重", "来源文件", "代次", "行号",
]

# request_id is used as a filename component; restrict it to a safe charset.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

STATUS_PENDING = "PENDING"
STATUS_GENERATING = "GENERATING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

_TMP_SUFFIX = ".tmp"


class VoucherError(ValueError):
    """Invalid voucher request parameters (HTTP 400/422 at the API layer)."""


class VoucherConflict(Exception):
    """Same request_id reused with different parameters (HTTP 409)."""

    def __init__(self, existing: dict):
        super().__init__("request_id was already accepted with different parameters")
        self.existing = existing


def _validate(request_id: str, sample_id: str, since: str, until: str) -> None:
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
        raise VoucherError(
            "request_id must be 1-128 chars of letters/digits/._-, starting "
            "with a letter or digit"
        )
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise VoucherError("sample_id is required")
    try:
        ts_since = datetime.fromisoformat(since)
        ts_until = datetime.fromisoformat(until)
    except ValueError as exc:
        raise VoucherError(f"since/until must be ISO-8601 timestamps: {exc}") from None
    if ts_since > ts_until:
        raise VoucherError("since must be earlier than or equal to until")


def _freeze_rows(conn: sqlite3.Connection, sample_id: str,
                 since: str, until: str) -> list[sqlite3.Row]:
    """Matching measurements at acceptance time. Joined to files inside the
    same transaction that inserts the voucher, so the snapshot is consistent
    with the voucher row and unaffected by later collections."""
    return conn.execute(
        "SELECT m.id AS measurement_id, m.raw, f.path AS source_path,"
        "       m.generation, m.line_no"
        " FROM measurements m JOIN files f ON f.id = m.file_id"
        " WHERE m.sample_id = ? AND m.weighed_at >= ? AND m.weighed_at <= ?"
        " ORDER BY m.id",
        (sample_id, since, until),
    ).fetchall()


def _original_three(raw: str) -> tuple[str, str, str]:
    """Extract the original three columns verbatim from the stored raw line.
    Collection guarantees exactly three comma-separated fields; the csv
    module also handles any quoted field correctly."""
    fields = next(csv.reader(io.StringIO(raw)))
    if len(fields) != 3:  # defensive: collection's parse_line already enforced this
        raise VoucherError(f"stored raw line does not have 3 columns: {raw!r}")
    return fields[0], fields[1], fields[2]


class VoucherManager:
    def __init__(self, db_path: str, output_dir: str,
                 fail_request_ids: set[str] | None = None,
                 crash_after_rename_ids: set[str] | None = None):
        self.db_path = db_path
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        # Demo/test hooks (see docker-compose.yml / README):
        # first generation attempt for these request ids fails / "crashes".
        self._fail_ids = set(fail_request_ids or [])
        self._crash_ids = set(crash_after_rename_ids or [])
        self._wake = threading.Condition()
        self._stop = False
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Recover any interrupted work, then run the background generator."""
        self.recover()
        self._worker = threading.Thread(
            target=self._run, name="voucher-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        with self._wake:
            self._stop = True
            self._wake.notify_all()
        if self._worker:
            self._worker.join(timeout=5)

    def _run(self) -> None:
        while True:
            with self._wake:
                if self._stop:
                    return
                self._wake.wait(timeout=2.0)
            try:
                if not self._process_one():
                    continue  # nothing to do; wait again
            except Exception as exc:  # noqa: BLE001 - worker must never die
                print(f"[voucher-worker] unexpected error: {exc!r}", flush=True)

    # ------------------------------------------------------------------ submission
    def submit(self, request_id: str, sample_id: str,
               since: str, until: str) -> tuple[dict, bool]:
        """Accept (or return the existing) voucher.

        Returns (voucher, created). Raises VoucherError on bad parameters,
        VoucherConflict when the id was already accepted with other params.
        """
        _validate(request_id, sample_id, since, until)
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")  # serializes concurrent first submits
            row = conn.execute(
                "SELECT * FROM vouchers WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is not None:
                conn.rollback()  # read-only path: release the write lock now
                if (row["sample_id"], row["since"], row["until"]) != (
                    sample_id, since, until
                ):
                    raise VoucherConflict(self.to_dict(row))
                return self.to_dict(row), False

            now = utcnow()
            cur = conn.execute(
                "INSERT INTO vouchers (request_id, sample_id, since, until,"
                " status, record_count, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (request_id, sample_id, since, until,
                 STATUS_PENDING, 0, now, now),
            )
            voucher_id = cur.lastrowid
            rows = _freeze_rows(conn, sample_id, since, until)
            for pos, r in enumerate(rows, start=1):
                col1, col2, col3 = _original_three(r["raw"])
                conn.execute(
                    "INSERT INTO voucher_records (voucher_id, position,"
                    " measurement_id, sample_id, weighed_at, grams,"
                    " source_path, generation, line_no)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (voucher_id, pos, r["measurement_id"], col1, col2, col3,
                     r["source_path"], r["generation"], r["line_no"]),
                )
            conn.execute(
                "UPDATE vouchers SET record_count=?, updated_at=? WHERE id=?",
                (len(rows), utcnow(), voucher_id),
            )
            conn.commit()
        finally:
            conn.close()

        voucher = self.get(request_id)
        with self._wake:
            self._wake.notify_all()
        return voucher, True

    # ------------------------------------------------------------------ queries
    def get(self, request_id: str) -> dict | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM vouchers WHERE request_id = ?", (request_id,)
            ).fetchone()
            return self.to_dict(row) if row else None
        finally:
            conn.close()

    def list(self, limit: int = 100) -> list[dict]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM vouchers ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
            return [self.to_dict(r) for r in rows]
        finally:
            conn.close()

    def records(self, request_id: str) -> list[dict]:
        """The frozen record set of a voucher."""
        conn = connect(self.db_path)
        try:
            vrow = conn.execute(
                "SELECT id FROM vouchers WHERE request_id = ?", (request_id,)
            ).fetchone()
            if vrow is None:
                return []
            rows = conn.execute(
                "SELECT position, measurement_id, sample_id, weighed_at, grams,"
                "       source_path, generation, line_no"
                " FROM voucher_records WHERE voucher_id = ? ORDER BY position",
                (vrow["id"],),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def download_path(self, request_id: str) -> str:
        """Path of a finished CSV. Raises FileNotFoundError if the voucher
        does not exist or is not COMPLETED (only complete files download)."""
        voucher = self.get(request_id)
        if voucher is None:
            raise FileNotFoundError(f"unknown request_id: {request_id}")
        if voucher["status"] != STATUS_COMPLETED or not voucher["file_path"]:
            raise FileNotFoundError(
                f"voucher {request_id} is {voucher['status']}, no file to download"
            )
        path = voucher["file_path"]
        if not os.path.isfile(path):
            # Defensive: COMPLETED row but file vanished from the volume.
            raise FileNotFoundError(f"voucher file missing on disk: {path}")
        return path

    # ------------------------------------------------------------------ retry
    def retry(self, request_id: str) -> dict:
        """Re-queue a FAILED (or still pending/generating) voucher using its
        frozen record set. A COMPLETED voucher is immutable and rejected."""
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM vouchers WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise FileNotFoundError(f"unknown request_id: {request_id}")
            if row["status"] == STATUS_COMPLETED:
                conn.rollback()
                raise VoucherError("voucher already completed; it is immutable")
            conn.execute(
                "UPDATE vouchers SET status=?, failure_reason=NULL,"
                " updated_at=? WHERE id=?",
                (STATUS_PENDING, utcnow(), row["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        with self._wake:
            self._wake.notify_all()
        return self.get(request_id)

    # ------------------------------------------------------------------ worker
    def _claim_next(self) -> sqlite3.Row | None:
        """Atomically take the oldest PENDING voucher."""
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM vouchers WHERE status = ? ORDER BY id LIMIT 1",
                (STATUS_PENDING,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            conn.execute(
                "UPDATE vouchers SET status=?, updated_at=? WHERE id=?",
                (STATUS_GENERATING, utcnow(), row["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return row

    def _process_one(self) -> bool:
        row = self._claim_next()
        if row is None:
            return False
        try:
            self._generate(row)
        except Exception as exc:  # noqa: BLE001 - record every failure, keep going
            self._mark_failed(row["id"], exc)
        return True

    def _generate(self, row: sqlite3.Row) -> None:
        voucher_id = row["id"]
        request_id = row["request_id"]
        final_path = self._file_path(request_id)
        tmp_path = final_path + _TMP_SUFFIX

        # The frozen snapshot, not the live measurements table, drives the CSV.
        conn = connect(self.db_path)
        try:
            recs = conn.execute(
                "SELECT position, measurement_id, sample_id, weighed_at, grams,"
                "       source_path, generation, line_no"
                " FROM voucher_records WHERE voucher_id = ? ORDER BY position",
                (voucher_id,),
            ).fetchall()
        finally:
            conn.close()

        # Demo/test hook: simulated write failure on the first attempt.
        if request_id in self._fail_ids:
            self._fail_ids.discard(request_id)
            raise OSError(f"injected write failure for voucher {request_id}")

        # Fresh partial temp each attempt; any stale one is overwritten.
        with open(tmp_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            for r in recs:
                writer.writerow([
                    r["measurement_id"], r["sample_id"], r["weighed_at"],
                    r["grams"], r["source_path"], r["generation"], r["line_no"],
                ])
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)  # atomic publish
        # Durability of the directory entry itself.
        try:
            dir_fd = os.open(self.output_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass

        # Demo/test hook: process dies AFTER the file is finished and renamed
        # but BEFORE the status is persisted. Recovery must adopt this file.
        if request_id in self._crash_ids:
            self._crash_ids.discard(request_id)
            os._exit(1)  # simulate a hard crash, bypassing all cleanup

        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM vouchers WHERE id=?", (voucher_id,)
            ).fetchone()
            if current is None or current["status"] != STATUS_GENERATING:
                conn.rollback()
                return  # voucher removed or retried concurrently; leave the file
            conn.execute(
                "UPDATE vouchers SET status=?, file_path=?, failure_reason=NULL,"
                " updated_at=? WHERE id=?",
                (STATUS_COMPLETED, final_path, utcnow(), voucher_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _mark_failed(self, voucher_id: int, exc: Exception) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE vouchers SET status=?, failure_reason=?, updated_at=?"
                " WHERE id=?",
                (STATUS_FAILED, reason[:1000], utcnow(), voucher_id),
            )
            conn.commit()
        finally:
            conn.close()
        # Remove the half-written artifact; a retry starts clean.
        tmp_path = self._file_path_by_id(voucher_id) + _TMP_SUFFIX
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ recovery
    def recover(self) -> None:
        """Run at startup.

        * GENERATING vouchers: if their finished CSV exists, adopt it
          (crash between rename and status save); otherwise re-queue.
        * PENDING vouchers: stay queued for the worker.
        * FAILED/COMPLETED: untouched.
        * Any leftover *.tmp is an unfinished artifact: delete it.
        """
        with os.scandir(self.output_dir) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(_TMP_SUFFIX):
                    try:
                        os.remove(entry.path)
                    except FileNotFoundError:
                        pass

        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM vouchers WHERE status = ?",
                (STATUS_GENERATING,),
            ).fetchall()
            for row in rows:
                final_path = self._file_path(row["request_id"])
                if os.path.isfile(final_path) and self._csv_matches(
                    final_path, row["id"], conn
                ):
                    # File completed before the crash; adopt it, do not rebuild.
                    conn.execute(
                        "UPDATE vouchers SET status=?, file_path=?,"
                        " failure_reason=NULL, updated_at=? WHERE id=?",
                        (STATUS_COMPLETED, final_path, utcnow(), row["id"]),
                    )
                    print(f"[voucher-recover] adopted finished file {final_path}",
                          flush=True)
                else:
                    conn.execute(
                        "UPDATE vouchers SET status=?, failure_reason=?,"
                        " updated_at=? WHERE id=?",
                        (STATUS_PENDING,
                         "interrupted by process exit before the file finished",
                         utcnow(), row["id"]),
                    )
                    print(f"[voucher-recover] re-queued voucher "
                          f"{row['request_id']}", flush=True)
            conn.commit()
        finally:
            conn.close()

    def _csv_matches(self, path: str, voucher_id: int,
                     conn: sqlite3.Connection) -> bool:
        """Verify an orphan final file really belongs to this voucher and is
        complete (header + exactly record_count data rows). Prevents adopting
        a stale file from a previous run."""
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.reader(fh)
                try:
                    header = next(reader)
                except StopIteration:
                    return False
                if header != CSV_HEADER:
                    return False
                data_rows = list(reader)
        except OSError:
            return False
        expected = conn.execute(
            "SELECT record_count FROM vouchers WHERE id=?", (voucher_id,)
        ).fetchone()["record_count"]
        if len(data_rows) != expected:
            return False
        frozen = conn.execute(
            "SELECT measurement_id FROM voucher_records"
            " WHERE voucher_id=? ORDER BY position", (voucher_id,)
        ).fetchall()
        return [r[0] for r in frozen] == [int(r[0]) for r in data_rows]

    # ------------------------------------------------------------------ helpers
    def _file_path(self, request_id: str) -> str:
        return os.path.join(self.output_dir, f"voucher_{request_id}.csv")

    def _file_path_by_id(self, voucher_id: int) -> str:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT request_id FROM vouchers WHERE id=?", (voucher_id,)
            ).fetchone()
        finally:
            conn.close()
        return self._file_path(row["request_id"]) if row else os.path.join(
            self.output_dir, f"voucher_unknown_{voucher_id}.csv"
        )

    @staticmethod
    def to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["downloadable"] = d["status"] == STATUS_COMPLETED and bool(d["file_path"])
        return d


def _main() -> None:
    """Minimal CLI used by tests/demos to exercise the hard-crash hook in a
    separate process: python voucher.py <db> <out_dir> <request_id> <sample>
    <since> <until> [crash|fail]
    """
    import sys

    db_path, out_dir, request_id, sample_id, since, until = sys.argv[1:7]
    mode = sys.argv[7] if len(sys.argv) > 7 else ""
    manager = VoucherManager(
        db_path, out_dir,
        fail_request_ids={request_id} if mode == "fail" else set(),
        crash_after_rename_ids={request_id} if mode == "crash" else set(),
    )
    manager.start()
    manager.submit(request_id, sample_id, since, until)
    # Give the worker time to reach the injected failure/crash point.
    for _ in range(250):
        time.sleep(0.05)
        v = manager.get(request_id)
        if v and v["status"] in (STATUS_COMPLETED, STATUS_FAILED):
            break
    manager.stop()


if __name__ == "__main__":
    _main()
