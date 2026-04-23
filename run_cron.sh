#!/usr/bin/env bash
# Usage:
#   ./run_cron.sh https://your-app.vercel.app          # normal — inserts snapshot
#   ./run_cron.sh http://localhost:3000 --dry-run       # dry run — prints snapshot, no DB insert

set -euo pipefail

BASE_URL="${1:?Usage: ./run_cron.sh <base-url> [--dry-run]}"
DRY_RUN=""
if [ "${2:-}" = "--dry-run" ]; then
  DRY_RUN="&dry=1"
  echo "DRY RUN — snapshot will be printed, not inserted into DB"
fi

echo "Starting fresh run..."
curl -s "${BASE_URL}/api/cron?reset=1${DRY_RUN}" | python3 -m json.tool

while true; do
  phase=$(curl -s "${BASE_URL}/api/status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('phase','unknown'))")

  if [ "$phase" = "complete" ]; then
    echo "Done!"
    break
  fi

  echo "Phase: $phase — processing next step..."
  curl -s "${BASE_URL}/api/cron?${DRY_RUN}" | python3 -m json.tool
  sleep 1
done
