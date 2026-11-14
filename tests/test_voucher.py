"""End-to-end tests of the handover-voucher core (voucher.py).

Covers every requirement in the voucher spec:
  1. first acceptance freezes the matching records and their sources
  2. same request_id + same params returns the original voucher (idempotent)
  3. same request_id + different params -> conflict
  4. concurrent first submissions generate exactly one voucher / one CSV
  5. late collection never changes an existing voucher (retry / restart too)
  6. status transitions, failure reason, retry, download-only-when-complete
  7. write failure -> FAILED, half-written temp cleaned up, retry succeeds
  8. crash between file completion and status save -> recovery adopts the file
  9. crash before completion -> recovery re-queues and rebuilds; tmp removed
 10. CSV content/order/header/BOM, frozen source path after rename
 11. empty match still produces a valid (header-only) voucher
"""

import csv
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))

import db as dbmod  # noqa: E402
from collector import collect_directory  # noqa: E402
from voucher import (  # noqa: E402
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_PENDING,
    VoucherConflict,
    VoucherError,
    VoucherManager,
)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def wait_status(manager, request_id, expected, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = manager.get(request_id)
        if v["status"] == expected:
            return v
        time.sleep(0.02)
    return manager.get(request_id)


def seed(tmp):
    """Create a db + incoming dir with three collected measurements."""
    incoming = os.path.join(tmp, "incoming")
    os.makedirs(incoming)
    db_path = os.path.join(tmp, "db", "measurements.db")
    out_dir = os.path.join(tmp, "vouchers")
    dbmod.init_db(db_path)
    cur = os.path.join(incoming, "weights_current.csv")
    with open(cur, "w", encoding="utf-8") as f:
        f.write("SMP-0001,2026-11-14T08:00:01,100.50\n")
        f.write("SMP-0001,2026-11-14T08:05:00,101.25\n")
        f.write("SMP-0002,2026-11-14T08:10:00,200.00\n")
    conn = dbmod.connect(db_path)
    collect_directory(conn, incoming)
    conn.close()
    return db_path, incoming, out_dir, cur


def read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def main():
    tmp = tempfile.mkdtemp(prefix="voucher-test-")
    db_path, incoming, out_dir, cur = seed(tmp)

    # --- 1. submit: snapshot frozen, background CSV completes ---------------
    mgr = VoucherManager(db_path, out_dir)
    mgr.start()
    v, created = mgr.submit("REQ-1", "SMP-0001",
                            "2026-11-14T08:00:00", "2026-11-14T09:00:00")
    check("first submit returns created=True", created is True)
    check("initial status PENDING", v["status"] == STATUS_PENDING)
    check("frozen record_count = 2", v["record_count"] == 2, str(v))
    v = wait_status(mgr, "REQ-1", STATUS_COMPLETED)
    check("background worker completes the voucher",
          v["status"] == STATUS_COMPLETED, str(v))
    check("file_path populated", bool(v["file_path"]) and os.path.isfile(v["file_path"]))
    check("downloadable flag set", v["downloadable"] is True)

    rows = read_csv(v["file_path"])
    check("CSV header (Chinese 7 columns)",
          rows[0] == ["测量编号", "样品编号", "称量时间", "克重", "来源文件", "代次", "行号"],
          str(rows[0]))
    check("CSV has 2 data rows", len(rows) == 3)
    check("row content: id + original three columns + source/generation/line",
          rows[1][1:4] == ["SMP-0001", "2026-11-14T08:00:01", "100.50"]
          and rows[1][4].endswith("weights_current.csv")
          and rows[1][5] == "0" and rows[1][6] == "1",
          str(rows[1]))
    check("BOM written for Excel",
          open(v["file_path"], "rb").read(3) == b"\xef\xbb\xbf")

    # --- 2. idempotent: same id + same params -> original voucher ------------
    v2, created2 = mgr.submit("REQ-1", "SMP-0001",
                              "2026-11-14T08:00:00", "2026-11-14T09:00:00")
    check("repeat submit created=False", created2 is False)
    check("same voucher id/file returned",
          v2["id"] == v["id"] and v2["file_path"] == v["file_path"])
    check("no duplicate CSV files",
          len([n for n in os.listdir(out_dir) if n.endswith(".csv")]) == 1)

    # --- 3. conflict: same id, different params ------------------------------
    try:
        mgr.submit("REQ-1", "SMP-0002",
                   "2026-11-14T08:00:00", "2026-11-14T09:00:00")
        check("conflicting params rejected", False)
    except VoucherConflict as exc:
        check("conflicting params -> 409-style conflict", True)
        check("conflict carries existing voucher",
              exc.existing["request_id"] == "REQ-1")
    try:
        mgr.submit("REQ-1", "SMP-0001",
                   "2026-11-14T08:00:00", "2026-11-14T08:30:00")
        check("different time window rejected", False)
    except VoucherConflict:
        check("different until -> conflict", True)

    # --- 4. parameter validation ---------------------------------------------
    for bad in [("", "S", "2026-01-01T00:00:00", "2026-01-02T00:00:00"),
                ("../escape", "S", "2026-01-01T00:00:00", "2026-01-02T00:00:00"),
                ("OK", "", "2026-01-01T00:00:00", "2026-01-02T00:00:00"),
                ("OK", "S", "not-a-time", "2026-01-02T00:00:00"),
                ("OK", "S", "2026-01-02T00:00:00", "2026-01-01T00:00:00")]:
        try:
            mgr.submit(*bad)
            check(f"bad params rejected: {bad[0]!r}", False)
        except VoucherError:
            check(f"bad params rejected: {bad[0]!r}", True)

    # --- 5. late collection ("补采") never alters the delivered voucher -------
    before = read_csv(v["file_path"])
    with open(cur, "a", encoding="utf-8") as f:
        f.write("SMP-0001,2026-11-14T08:30:00,999.99\n")  # late, within window
    conn = dbmod.connect(db_path)
    r = collect_directory(conn, incoming)
    conn.close()
    check("late record collected into the live db", r.measurements == 1)
    v3, _ = mgr.submit("REQ-1", "SMP-0001",
                       "2026-11-14T08:00:00", "2026-11-14T09:00:00")
    check("voucher record_count unchanged after late collection",
          v3["record_count"] == 2)
    check("voucher CSV unchanged after late collection",
          read_csv(v3["file_path"]) == before)
    recs = mgr.records("REQ-1")
    check("frozen record set still has 2 entries", len(recs) == 2)
    check("frozen rows expose measurement id / source / generation / line",
          recs[0]["measurement_id"] == 1 and recs[0]["line_no"] == 1
          and recs[0]["source_path"].endswith("weights_current.csv"))
    check("download path available", mgr.download_path("REQ-1") == v["file_path"])

    # --- 6. source path frozen even when the source file is renamed ----------
    # Rotate the source file like the instrument does, then re-collect.
    archived = os.path.join(incoming, "weights_arch.csv")
    os.rename(cur, archived)
    open(cur, "a", encoding="utf-8").close()
    conn = dbmod.connect(db_path)
    collect_directory(conn, incoming)
    conn.close()
    check("frozen source path survives source-file rename",
          mgr.records("REQ-1")[0]["source_path"].endswith("weights_current.csv"))
    check("CSV file itself untouched by rename", read_csv(v["file_path"]) == before)

    # --- 7. write failure -> FAILED, reason exposed, temp removed, retry -----
    # The injected failure runs in a child process so its state is exactly
    # what a real crashed/failed service would leave behind on disk.
    child = subprocess.run(
        [sys.executable, "-I", os.path.join(APP_DIR, "voucher.py"),
         db_path, out_dir, "REQ-FAIL", "SMP-0002",
         "2026-11-14T08:00:00", "2026-11-14T09:00:00", "fail"],
        cwd=APP_DIR, capture_output=True, text=True, timeout=30,
    )
    check("failure child ran", child.returncode == 0, child.stderr)
    fail_mgr = VoucherManager(db_path, out_dir)
    fail_mgr.start()
    fv = fail_mgr.get("REQ-FAIL")
    check("injected failure -> FAILED", fv["status"] == STATUS_FAILED, str(fv))
    check("failure_reason recorded", bool(fv["failure_reason"])
          and "injected write failure" in fv["failure_reason"])
    check("not downloadable while FAILED", fv["downloadable"] is False)
    try:
        fail_mgr.download_path("REQ-FAIL")
        check("download of FAILED voucher refused", False)
    except FileNotFoundError:
        check("download of FAILED voucher refused", True)
    check("half-written temp file cleaned up",
          not os.path.exists(os.path.join(out_dir, "voucher_REQ-FAIL.csv.tmp")))
    rv = fail_mgr.retry("REQ-FAIL")
    check("retry re-queues (PENDING)", rv["status"] == STATUS_PENDING
          and rv["failure_reason"] is None)
    rv = wait_status(fail_mgr, "REQ-FAIL", STATUS_COMPLETED)
    check("retry succeeds with the same frozen record set",
          rv["status"] == STATUS_COMPLETED and rv["record_count"] == 1)
    check("retry produced one final CSV, no temp left",
          os.path.isfile(rv["file_path"])
          and not os.path.exists(rv["file_path"] + ".tmp"))
    try:
        fail_mgr.retry("REQ-FAIL")
        check("retry of COMPLETED voucher refused", False)
    except VoucherError:
        check("retry of COMPLETED voucher refused (immutable)", True)

    # --- 8. concurrent first submissions -> exactly one voucher --------------
    errors, results = [], []

    def worker():
        try:
            voucher, _ = mgr.submit("REQ-CONC", "SMP-0001",
                                    "2026-11-14T08:00:00",
                                    "2026-11-14T09:00:00")
            results.append(voucher["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("no errors under concurrent submit", not errors, str(errors[:1]))
    check("all threads got the same voucher id",
          len(set(results)) == 1 and len(results) == 8, str(results))
    cv = wait_status(mgr, "REQ-CONC", STATUS_COMPLETED)
    check("concurrent voucher completed once", cv["status"] == STATUS_COMPLETED)
    csvs = [n for n in os.listdir(out_dir) if n.endswith(".csv")]
    check("exactly one CSV per request id, no temps",
          sorted(csvs) == ["voucher_REQ-1.csv", "voucher_REQ-CONC.csv",
                           "voucher_REQ-FAIL.csv"]
          and not [n for n in os.listdir(out_dir) if n.endswith(".tmp")],
          str(os.listdir(out_dir)))

    # --- 9. crash AFTER rename, BEFORE status save -> recovery adopts file ---
    # The child hard-exits (os._exit) after the CSV is fully written/renamed
    # but before the COMPLETED status is committed.
    child = subprocess.run(
        [sys.executable, "-I", os.path.join(APP_DIR, "voucher.py"),
         db_path, out_dir, "REQ-CRASH", "SMP-0001",
         "2026-11-14T08:00:00", "2026-11-14T09:00:00", "crash"],
        cwd=APP_DIR, capture_output=True, text=True, timeout=30,
    )
    check("crash child exited hard (non-zero)", child.returncode != 0,
          f"rc={child.returncode}")
    check("crashed voucher's finished CSV exists on disk",
          os.path.isfile(os.path.join(out_dir, "voucher_REQ-CRASH.csv")))
    crash_mgr = VoucherManager(db_path, out_dir)
    check("row left GENERATING by the crash",
          crash_mgr.get("REQ-CRASH")["status"] == STATUS_GENERATING)
    # A brand-new manager = process restart; recover() runs in start().
    restarted = VoucherManager(db_path, out_dir)
    restarted.start()
    rv2 = restarted.get("REQ-CRASH")
    check("restart recovery adopts the finished file (no rebuild)",
          rv2["status"] == STATUS_COMPLETED
          and rv2["file_path"].endswith("voucher_REQ-CRASH.csv"),
          str(rv2))
    check("adopted CSV content matches the frozen snapshot",
          read_csv(rv2["file_path"]) == [
              ["测量编号", "样品编号", "称量时间", "克重", "来源文件", "代次", "行号"],
              *([[str(r["measurement_id"]), r["sample_id"], r["weighed_at"],
                  r["grams"], r["source_path"], str(r["generation"]),
                  str(r["line_no"])]
                 for r in restarted.records("REQ-CRASH")])
          ])
    crash_mgr.stop()

    # --- 10. crash BEFORE completion -> re-queue, temp swept, rebuild --------
    conn = dbmod.connect(db_path)
    conn.execute("INSERT INTO vouchers (request_id, sample_id, since, until,"
                 " status, record_count, created_at, updated_at)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 ("REQ-EARLY", "SMP-0001", "2026-11-14T08:00:00",
                  "2026-11-14T09:00:00", STATUS_GENERATING, 2,
                  dbmod.utcnow(), dbmod.utcnow()))
    vid = conn.execute(
        "SELECT id FROM vouchers WHERE request_id=?", ("REQ-EARLY",)
    ).fetchone()[0]
    conn.execute("INSERT INTO voucher_records (voucher_id, position,"
                 " measurement_id, sample_id, weighed_at, grams,"
                 " source_path, generation, line_no)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (vid, 1, 1, "SMP-0001", "2026-11-14T08:00:01", "100.50",
                  "/x/weights_current.csv", 0, 1))
    conn.execute("INSERT INTO voucher_records (voucher_id, position,"
                 " measurement_id, sample_id, weighed_at, grams,"
                 " source_path, generation, line_no)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (vid, 2, 2, "SMP-0001", "2026-11-14T08:05:00", "101.25",
                  "/x/weights_current.csv", 0, 2))
    conn.commit()
    conn.close()
    with open(os.path.join(out_dir, "voucher_REQ-EARLY.csv.tmp"), "w") as f:
        f.write("partial,half")  # unfinished artifact
    mgr2 = VoucherManager(db_path, out_dir)
    mgr2.start()
    check("stale temp swept at startup",
          not os.path.exists(os.path.join(out_dir, "voucher_REQ-EARLY.csv.tmp")))
    ev = wait_status(mgr2, "REQ-EARLY", STATUS_PENDING)
    check("interrupted voucher re-queued with reason",
          ev["status"] == STATUS_PENDING and bool(ev["failure_reason"]))
    ev = wait_status(mgr2, "REQ-EARLY", STATUS_COMPLETED)
    check("re-queued voucher rebuilt and completed",
          ev["status"] == STATUS_COMPLETED and os.path.isfile(ev["file_path"]))
    check("rebuilt CSV complete", len(read_csv(ev["file_path"])) == 3)

    # --- 11. empty match: header-only voucher still completes ---------------
    mgr2.submit("REQ-EMPTY", "SMP-NOPE",
                "2026-11-14T08:00:00", "2026-11-14T09:00:00")
    ev2 = wait_status(mgr2, "REQ-EMPTY", STATUS_COMPLETED)
    check("zero-match voucher completes", ev2["status"] == STATUS_COMPLETED
          and ev2["record_count"] == 0)
    check("zero-match CSV is header-only",
          read_csv(ev2["file_path"]) == [
              ["测量编号", "样品编号", "称量时间", "克重", "来源文件", "代次", "行号"]])

    # --- 12. list / unknown id behaviour -------------------------------------
    ids = {v["request_id"] for v in mgr2.list(limit=100)}
    check("list exposes all vouchers",
          {"REQ-1", "REQ-CONC", "REQ-FAIL", "REQ-CRASH",
           "REQ-EARLY", "REQ-EMPTY"} <= ids, str(ids))
    check("unknown id -> None / download error",
          mgr2.get("NO-SUCH") is None)
    try:
        mgr2.download_path("NO-SUCH")
        check("unknown download refused", False)
    except FileNotFoundError:
        check("unknown download refused", True)

    for m in (mgr, fail_mgr, restarted, mgr2):
        m.stop()
    print(f"\n{PASS} passed, {FAIL} failed  (tmp: {tmp})")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
