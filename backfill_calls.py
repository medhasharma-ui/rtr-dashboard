#!/usr/bin/env python3
"""
One-time backfill for stale call rows.

Calls are first synced while still 'in-progress' (duration 0). Before the
date_updated__gte fix in sync_events.py, the incremental sync filtered on
date_created, so a call that completed after its initial sync was never
re-fetched and its row stayed stale forever.

This script finds all calls still in a non-terminal status, re-fetches each
by id from Close (which now reflects the completed duration/status), and
upserts the fresh values.

Usage:
  python3 backfill_calls.py

Requires: CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY env vars.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

from pull_data import get_api_key, close_get, MAX_WORKERS
from db import get_supabase, upsert_calls

# Calls in these statuses may still complete after their initial sync.
STALE_STATUSES = ["in-progress", "created"]


def fetch_call(api_key, call_id):
    """Re-fetch a single call activity by id. Returns dict or None if gone."""
    try:
        return close_get(
            f"activity/call/{call_id}/",
            params={"_fields": "id,lead_id,user_id,date_created,duration,status"},
            api_key=api_key,
        )
    except Exception as e:
        print(f"  Warning: failed to fetch {call_id}: {e}")
        return None


def main():
    api_key = get_api_key()
    sb = get_supabase()

    print("Finding stale call rows...")
    stale_ids = []
    for status in STALE_STATUSES:
        rows = sb.table("calls").select("id").eq("status", status).execute()
        stale_ids.extend(r["id"] for r in rows.data)
    print(f"  {len(stale_ids)} calls in {STALE_STATUSES}")

    if not stale_ids:
        print("Nothing to backfill.")
        return

    print(f"Re-fetching {len(stale_ids)} calls from Close...")
    t0 = time.time()
    fresh = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_call, api_key, cid): cid for cid in stale_ids}
        for future in as_completed(futures):
            call = future.result()
            if call:
                fresh.append(call)
    print(f"  Fetched {len(fresh)} calls ({time.time()-t0:.1f}s)")

    changed = sum(1 for c in fresh if c.get("status") not in STALE_STATUSES)
    print(f"  {changed} have since completed / changed status")

    count = upsert_calls(sb, fresh)
    print(f"Upserted {count} calls.")


if __name__ == "__main__":
    main()
