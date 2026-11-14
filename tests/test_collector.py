"""End-to-end tests of the collector core against a temp directory.

Covers: partial lines, rotation-by-rename, late writes to rotated files,
restart durability, concurrent runs, truncation/inode reuse, bad lines,
and repeated weighings of the same sample.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import db as dbmod  # noqa: E402
from collector import collect_directory  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def count(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="collector-test-")
    incoming = os.path.join(tmp, "incoming")
    os.makedirs(incoming)
    db_path = os.path.join(tmp, "db", "measurements.db")
    dbmod.init_db(db_path)
    cur = os.path.join(incoming, "weights_current.csv")

    # --- 1. partial line must wait, complete lines collected ---------------
    with open(cur, "w", encoding="utf-8") as f:
        f.write("SMP-0001,2026-11-13T08:00:01,100.50\n")
        f.write("SMP-0002,2026-11-13T08:00:02,200.25\n")
        f.write("SMP-0003,2026-11-13T08:00:03,300.")   # half line, no newline
    conn = dbmod.connect(db_path)
    r1 = collect_directory(conn, incoming)
    conn.close()
    check("partial line held back", r1.measurements == 2, f"got {r1.measurements}")
    check("2 measurements stored",
          count(db_path, "SELECT COUNT(*) FROM measurements") == 2)

    # same collect again -> idempotent, nothing new
    conn = dbmod.connect(db_path)
    r1b = collect_directory(conn, incoming)
    conn.close()
    check("re-collect is a no-op", r1b.measurements == 0)

    # --- 2. instrument completes the half line ------------------------------
    with open(cur, "a", encoding="utf-8") as f:
        f.write("75\n")   # completes SMP-0003 line
        f.write("SMP-0001,2026-11-13T08:00:04,101.00\n")  # same sample again
    conn = dbmod.connect(db_path)
    r2 = collect_directory(conn, incoming)
    conn.close()
    check("completed half line collected", r2.measurements == 2)
    check("same sample weighed twice kept",
          count(db_path, "SELECT COUNT(*) FROM measurements WHERE sample_id='SMP-0001'") == 2)
    g = rows(db_path, "SELECT grams FROM measurements WHERE sample_id='SMP-0003'")
    check("half line parsed correctly", len(g) == 1 and abs(g[0]["grams"] - 300.75) < 1e-9,
          f"got {g}")

    # --- 3. rotation: rename current, create new, late-write to old ---------
    archived = os.path.join(incoming, "weights_20261113.csv")
    os.rename(cur, archived)                      # rotation rename
    with open(cur, "w", encoding="utf-8") as f:   # new day file
        f.write("SMP-0004,2026-11-14T00:00:01,10.00\n")
    with open(archived, "a", encoding="utf-8") as f:  # late records on old file
        f.write("SMP-0005,2026-11-13T23:59:59,55.55\n")
    conn = dbmod.connect(db_path)
    r3 = collect_directory(conn, incoming)
    conn.close()
    check("rotation: new + late lines collected", r3.measurements == 2,
          f"got {r3.measurements}")
    total = count(db_path, "SELECT COUNT(*) FROM measurements")
    check("no duplicates after rename", total == 6, f"total={total}")

    # collect again: rotated file must NOT be re-read
    conn = dbmod.connect(db_path)
    r3b = collect_directory(conn, incoming)
    conn.close()
    check("renamed file not re-collected", r3b.measurements == 0)
    check("total still 6", count(db_path, "SELECT COUNT(*) FROM measurements") == 6)

    # --- 4. restart durability: brand-new connection, same db ---------------
    conn = dbmod.connect(db_path)
    r4 = collect_directory(conn, incoming)
    conn.close()
    check("after 'restart' no re-collection", r4.measurements == 0)

    # --- 5. concurrent collectors -------------------------------------------
    with open(cur, "a", encoding="utf-8") as f:
        for i in range(50):
            f.write(f"SMP-C{i:03d},2026-11-14T01:{i // 60:02d}:{i % 60:02d},{i}.0\n")
    errors = []

    def worker():
        try:
            c = dbmod.connect(db_path)
            collect_directory(c, incoming)
            c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("no errors under concurrency", not errors, f"{errors[:1]}")
    check("concurrent runs: exactly 56 rows, no dup/gap",
          count(db_path, "SELECT COUNT(*) FROM measurements") == 56,
          f"got {count(db_path, 'SELECT COUNT(*) FROM measurements')}")
    check("line numbers unique per file",
          count(db_path, """SELECT COUNT(*) FROM (
                SELECT file_id, generation, line_no FROM measurements
                GROUP BY file_id, generation, line_no HAVING COUNT(*) > 1)""") == 0)

    # --- 6. truncation / inode reuse -> new generation ----------------------
    with open(cur, "w", encoding="utf-8") as f:   # truncate in place
        f.write("SMP-0009,2026-11-14T02:00:00,9.99\n")
    conn = dbmod.connect(db_path)
    r6 = collect_directory(conn, incoming)
    conn.close()
    check("truncated file re-read from 0", r6.measurements == 1)
    check("old rows survive truncation",
          count(db_path, "SELECT COUNT(*) FROM measurements") == 57)
    gens = rows(db_path, "SELECT DISTINCT generation FROM measurements")
    check("two generations recorded", len(gens) == 2, f"{gens}")

    # --- 7. malformed complete line -> rejected, not lost -------------------
    with open(cur, "a", encoding="utf-8") as f:
        f.write("BROKEN-LINE-WITHOUT-FIELDS\n")
        f.write("SMP-0010,2026-11-14T03:00:00,not-a-number\n")
        f.write("SMP-0011,2026-11-14T03:00:01,11.11\n")
    conn = dbmod.connect(db_path)
    r7 = collect_directory(conn, incoming)
    conn.close()
    check("good line after bad ones collected", r7.measurements == 1)
    check("2 rejected lines recorded",
          count(db_path, "SELECT COUNT(*) FROM rejected_lines") == 2)
    check("rejected lines not reprocessed",
          collect_directory(dbmod.connect(db_path), incoming).rejected == 0)

    # --- 8. CRLF tolerance ---------------------------------------------------
    with open(cur, "ab") as f:
        f.write("SMP-0012,2026-11-14T04:00:00,12.34\r\n".encode())
    conn = dbmod.connect(db_path)
    r8 = collect_directory(conn, incoming)
    conn.close()
    check("CRLF line parsed", r8.measurements == 1)

    # --- 9. file identity across renames (path changes, data doesn't) -------
    frows = rows(db_path, "SELECT dev, ino, path FROM files ORDER BY id")
    check("archived file tracked once", len(frows) == 2, f"{frows}")
    check("archived path updated after rename",
          any(r["path"].endswith("weights_20261113.csv") for r in frows))

    print(f"\n{PASS} passed, {FAIL} failed  (tmp: {tmp})")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
