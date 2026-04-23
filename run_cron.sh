#!/usr/bin/env bash
# Usage:
#   ./run_cron.sh https://your-app.vercel.app mtd              # MTD pull — inserts snapshot
#   ./run_cron.sh https://your-app.vercel.app recent            # recent pull (today+yesterday)
#   ./run_cron.sh http://localhost:3000 mtd --dry-run           # dry run — prints snapshot, no DB insert

set -euo pipefail

BASE_URL="${1:?Usage: ./run_cron.sh <base-url> <mtd|recent> [--dry-run]}"
MODE="${2:?Usage: ./run_cron.sh <base-url> <mtd|recent> [--dry-run]}"
DRY_RUN=""
if [ "${3:-}" = "--dry-run" ]; then
  DRY_RUN="&dry=1"
  echo "DRY RUN — snapshot will be printed, not inserted into DB"
fi

echo "Starting fresh ${MODE} run..."
curl -s "${BASE_URL}/api/cron?reset=1&mode=${MODE}${DRY_RUN}" | python3 -m json.tool

while true; do
  phase=$(curl -s "${BASE_URL}/api/status?mode=${MODE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('phase','unknown'))")

  if [ "$phase" = "complete" ]; then
    echo "Done!"
    break
  fi

  echo "Phase: $phase — processing next step..."
  curl -s "${BASE_URL}/api/cron?mode=${MODE}${DRY_RUN}" | python3 -m json.tool
  sleep 1
done
