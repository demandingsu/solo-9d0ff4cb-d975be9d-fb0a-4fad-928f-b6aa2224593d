"""Local weighing-instrument simulator.

Mimics an instrument that:
  * appends 'sample_id,weighed_at,grams' lines (UTF-8, no header) to the
    current day file;
  * sometimes flushes only half a line and completes it a moment later
    (partial-line demo);
  * rotates daily (accelerated via ROTATE_SECONDS): renames the current
    file to an archive name, then creates a fresh current file;
  * keeps appending a few late records to the ROTATED file afterwards
    (late-write demo).

All behaviour is configurable through environment variables.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta

INCOMING_DIR = os.environ.get("INCOMING_DIR", "/data/incoming")
WRITE_INTERVAL = float(os.environ.get("WRITE_INTERVAL_SECONDS", "1.0"))
ROTATE_EVERY = float(os.environ.get("ROTATE_SECONDS", "45"))
PARTIAL_EVERY = int(os.environ.get("PARTIAL_EVERY_N_LINES", "7"))
LATE_LINES = int(os.environ.get("LATE_LINES_AFTER_ROTATION", "2"))

CURRENT_NAME = "weights_current.csv"


def log(msg: str) -> None:
    print(f"[simulator {datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def fmt_ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def make_line(seq: int, when: datetime | None = None) -> str:
    when = when or datetime.now()
    sample = f"SMP-{random.randint(1, 12):04d}"
    grams = f"{random.uniform(0.5, 500.0):.2f}"
    return f"{sample},{fmt_ts(when)},{grams}"


def append(path: str, data: str) -> None:
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def rotate(current: str, day_seq: int) -> str:
    """Rename current file to an archive name, create a fresh current file."""
    stamp = datetime.now().strftime("%Y%m%d")
    archived = os.path.join(
        INCOMING_DIR, f"weights_{stamp}_day{day_seq:03d}.csv"
    )
    os.rename(current, archived)
    open(current, "a", encoding="utf-8").close()  # instrument creates new day file
    log(f"ROTATE: {os.path.basename(current)} -> {os.path.basename(archived)},"
        f" new current file created")
    return archived


def main() -> None:
    os.makedirs(INCOMING_DIR, exist_ok=True)
    current = os.path.join(INCOMING_DIR, CURRENT_NAME)
    if not os.path.exists(current):
        open(current, "a", encoding="utf-8").close()
    log(f"writing to {current}; rotate every {ROTATE_EVERY}s,"
        f" partial line every {PARTIAL_EVERY} writes")

    seq = 0
    day_seq = 0
    last_rotate = time.monotonic()
    rotated_files: list[str] = []

    while True:
        now = time.monotonic()
        if now - last_rotate >= ROTATE_EVERY:
            day_seq += 1
            archived = rotate(current, day_seq)
            rotated_files.append(archived)
            last_rotate = now
            # Late records for the 'previous day' keep arriving on the old file.
            for i in range(LATE_LINES):
                seq += 1
                late_ts = datetime.now() - timedelta(seconds=ROTATE_EVERY + i)
                line = make_line(seq, late_ts)
                append(archived, line + "\n")
                log(f"LATE-WRITE to rotated {os.path.basename(archived)}: {line}")
            time.sleep(WRITE_INTERVAL)
            continue

        seq += 1
        line = make_line(seq)
        if seq % PARTIAL_EVERY == 0:
            # Half-line write: flush the first part without a newline,
            # finish it after a pause. Collectors must not ingest it yet.
            cut = max(1, len(line) // 2)
            append(current, line[:cut])
            log(f"PARTIAL write ({cut}/{len(line)} bytes, no newline): {line[:cut]!r}")
            time.sleep(WRITE_INTERVAL)
            append(current, line[cut:] + "\n")
            log(f"PARTIAL completed: {line!r}")
        else:
            append(current, line + "\n")
            log(f"write: {line}")
        time.sleep(WRITE_INTERVAL)


if __name__ == "__main__":
    main()
