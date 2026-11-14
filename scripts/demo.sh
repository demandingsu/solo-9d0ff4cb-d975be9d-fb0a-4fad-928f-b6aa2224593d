#!/usr/bin/env bash
# Demo walkthrough: half-line writes, rotation with late appends,
# API collection and queries. Assumes `docker compose up` is running.
set -euo pipefail

API=${API:-http://localhost:8000}
INCOMING=${INCOMING_DIR:-}

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "health"
curl -s "$API/health" | python3 -m json.tool

step "collect whatever the simulator has written so far"
curl -s -X POST "$API/collect" -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool

step "measurements so far (first 5)"
curl -s "$API/measurements?limit=5" | python3 -m json.tool

step "tracked files (note offsets; rotated files keep their identity)"
curl -s "$API/files" | python3 -m json.tool

if [ -n "$INCOMING" ] && [ -d "$INCOMING" ]; then
  CUR="$INCOMING/weights_current.csv"
  step "write a HALF line manually (no newline), then collect -> must NOT appear"
  printf 'SMP-DEMO1,2026-11-13T10:00:00,42.0' >> "$CUR"
  curl -s -X POST "$API/collect" -H 'Content-Type: application/json' -d '{}' \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); print("inserted:", r["measurements_inserted"])'
  curl -s "$API/measurements?sample_id=SMP-DEMO1" \
    | python3 -c 'import json,sys; print("SMP-DEMO1 rows:", json.load(sys.stdin)["total"])'

  step "complete the half line, collect again -> appears exactly once"
  printf '\n' >> "$CUR"
  curl -s -X POST "$API/collect" -H 'Content-Type: application/json' -d '{}' \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); print("inserted:", r["measurements_inserted"])'
  curl -s "$API/measurements?sample_id=SMP-DEMO1" \
    | python3 -c 'import json,sys; print("SMP-DEMO1 rows:", json.load(sys.stdin)["total"])'

  step "rotate manually: rename current, append a LATE line to the old file"
  OLD="$INCOMING/weights_manual_$(date +%s).csv"
  mv "$CUR" "$OLD"
  touch "$CUR"
  printf 'SMP-DEMO2,2026-11-13T09:59:59,7.77\n' >> "$OLD"   # late write to rotated file
  printf 'SMP-DEMO3,2026-11-13T10:00:01,8.88\n' >> "$CUR"   # first line of new file
  curl -s -X POST "$API/collect" -H 'Content-Type: application/json' -d '{}' \
    | python3 -c 'import json,sys; r=json.load(sys.stdin); print("inserted:", r["measurements_inserted"])'
  curl -s "$API/measurements?sample_id=SMP-DEMO2" \
    | python3 -c 'import json,sys; print("late line on rotated file, rows:", json.load(sys.stdin)["total"])'
fi

step "collect again -> idempotent, nothing new"
curl -s -X POST "$API/collect" -H 'Content-Type: application/json' -d '{}' \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("inserted:", r["measurements_inserted"])'

step "query by sample and time range"
curl -s "$API/measurements?sample_id=SMP-0001&limit=3" | python3 -m json.tool
echo
echo "Demo done. Try: curl '$API/measurements?since=2026-11-13T00:00:00&limit=10'"
