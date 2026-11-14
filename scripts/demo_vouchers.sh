#!/usr/bin/env bash
# Voucher (handover certificate) demo walkthrough.
#
# Assumes `docker compose up --build` is running. Everything below can also be
# done by hand in the Swagger UI at http://localhost:8000/docs.
#
# Scenarios:
#   1. normal generation (background) + idempotent replay + 409 conflict
#   2. supplementary collection ("补采") never changes a delivered voucher
#   3. write failure -> FAILED + reason, then successful retry
#   4. hard crash after the CSV was finished -> restart recovery adopts it
set -euo pipefail

API=${API:-http://localhost:8000}
PY=python3

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
jget() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print(eval(\"d$1\"))"; }

# Pick a sample id that already has measurements, and a window covering today.
step "collect everything the simulator has written so far"
curl -s -X POST "$API/collect" -H 'Content-Type: application/json' -d '{}' \
  | "$PY" -m json.tool | head -8

SAMPLE=$(curl -s "$API/measurements?limit=1" | jget "['items'][0]['sample_id']")
SINCE=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S 2>/dev/null \
        || date -u -v-1H +%Y-%m-%dT%H:%M:%S)
UNTIL=$(date -u -d '1 hour' +%Y-%m-%dT%H:%M:%S 2>/dev/null \
        || date -u -v+1H +%Y-%m-%dT%H:%M:%S)
echo "using sample_id=$SAMPLE window=[$SINCE, $UNTIL]"

# ---------------------------------------------------------------- scenario 1
step "1a. first submission: matching records are frozen, CSV generated in background"
curl -s -X POST "$API/vouchers" -H 'Content-Type: application/json' \
  -d "{\"request_id\":\"demo-1\",\"sample_id\":\"$SAMPLE\",\"since\":\"$SINCE\",\"until\":\"$UNTIL\"}" \
  | "$PY" -m json.tool

step "1b. poll status until COMPLETED"
for _ in $(seq 1 25); do
  STATUS=$(curl -s "$API/vouchers/demo-1" | jget "['status']")
  echo "  status=$STATUS"
  [ "$STATUS" = COMPLETED ] && break
  sleep 1
done

step "1c. same request_id + same parameters -> original voucher (created=false)"
curl -s -X POST "$API/vouchers" -H 'Content-Type: application/json' \
  -d "{\"request_id\":\"demo-1\",\"sample_id\":\"$SAMPLE\",\"since\":\"$SINCE\",\"until\":\"$UNTIL\"}" \
  | "$PY" -m json.tool

step "1d. same request_id, different parameters -> 409 conflict"
curl -s -o /tmp/conflict.json -w "HTTP %{http_code}\n" \
  -X POST "$API/vouchers" -H 'Content-Type: application/json' \
  -d "{\"request_id\":\"demo-1\",\"sample_id\":\"OTHER\",\"since\":\"$SINCE\",\"until\":\"$UNTIL\"}"
"$PY" -m json.tool /tmp/conflict.json

step "1e. voucher detail incl. the frozen record set (sources/generations/lines)"
curl -s "$API/vouchers/demo-1" | "$PY" -m json.tool | head -40

step "1f. download the completed CSV (only COMPLETED vouchers are downloadable)"
curl -s -D - "$API/vouchers/demo-1/download" -o /tmp/demo-1.csv | head -4
echo "--- CSV ---"; cat /tmp/demo-1.csv; echo "-----------"

# ---------------------------------------------------------------- scenario 2
step "2a. supplementary collection: trigger /collect again (late records arrive)"
curl -s -X POST "$API/collect" -H 'Content-Type: application/json' -d '{}' \
  | "$PY" -c "import json,sys; print('inserted:', json.load(sys.stdin)['measurements_inserted'])"

step "2b. re-request the same voucher: record set and CSV stay identical"
BEFORE=$(sha256sum /tmp/demo-1.csv | cut -d' ' -f1)
curl -s -X POST "$API/vouchers" -H 'Content-Type: application/json' \
  -d "{\"request_id\":\"demo-1\",\"sample_id\":\"$SAMPLE\",\"since\":\"$SINCE\",\"until\":\"$UNTIL\"}" \
  | jget "['voucher']['record_count']"
curl -s "$API/vouchers/demo-1/download" -o /tmp/demo-1-after.csv
AFTER=$(sha256sum /tmp/demo-1-after.csv | cut -d' ' -f1)
[ "$BEFORE" = "$AFTER" ] && echo "PASS: voucher CSV unchanged after 补采" \
  || { echo "FAIL: voucher changed!"; exit 1; }

# ---------------------------------------------------------------- scenario 3
step "3a. demo-fail is configured (VOUCHER_FAIL_REQUEST_IDS) to fail once -> FAILED"
curl -s -X POST "$API/vouchers" -H 'Content-Type: application/json' \
  -d "{\"request_id\":\"demo-fail\",\"sample_id\":\"$SAMPLE\",\"since\":\"$SINCE\",\"until\":\"$UNTIL\"}" \
  > /dev/null
for _ in $(seq 1 25); do
  STATUS=$(curl -s "$API/vouchers/demo-fail" | jget "['status']")
  [ "$STATUS" = FAILED ] && break
  sleep 1
done
curl -s "$API/vouchers/demo-fail" | "$PY" -m json.tool
echo "download attempt on FAILED voucher:"
curl -s -o /dev/null -w "  HTTP %{http_code} (expected 404)\n" \
  "$API/vouchers/demo-fail/download"

step "3b. retry -> re-queued with the SAME frozen records, then COMPLETED"
curl -s -X POST "$API/vouchers/demo-fail/retry" | "$PY" -m json.tool
for _ in $(seq 1 25); do
  STATUS=$(curl -s "$API/vouchers/demo-fail" | jget "['status']")
  echo "  status=$STATUS"
  [ "$STATUS" = COMPLETED ] && break
  sleep 1
done
curl -s "$API/vouchers/demo-fail/download" | head -3

# ---------------------------------------------------------------- scenario 4
step "4a. demo-crash: process hard-exits AFTER the CSV is finished but BEFORE status is saved"
curl -s -X POST "$API/vouchers" -H 'Content-Type: application/json' \
  -d "{\"request_id\":\"demo-crash\",\"sample_id\":\"$SAMPLE\",\"since\":\"$SINCE\",\"until\":\"$UNTIL\"}" \
  > /dev/null
echo "  waiting for the app container to crash and be restarted by Docker ..."
for _ in $(seq 1 30); do
  sleep 2
  HEALTH=$(curl -s -o /dev/null -w '%{http_code}' "$API/health" || true)
  [ "$HEALTH" = 200 ] || { echo "  app restarting ..."; continue; }
  STATUS=$(curl -s "$API/vouchers/demo-crash" | jget "['status']" || true)
  echo "  after restart: status=$STATUS"
  [ "$STATUS" = COMPLETED ] && break
done
step "4b. recovered voucher is downloadable; the finished file was adopted, not rebuilt"
curl -s "$API/vouchers/demo-crash" | "$PY" -m json.tool | grep -E 'status|file_path|record_count'
curl -s -D - "$API/vouchers/demo-crash/download" -o /tmp/demo-crash.csv | grep -i content-disposition
head -3 /tmp/demo-crash.csv

step "voucher list"
curl -s "$API/vouchers" | "$PY" -m json.tool

echo
echo "Demo done. Browse http://localhost:8000/docs for the interactive API."
