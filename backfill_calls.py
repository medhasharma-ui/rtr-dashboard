#!/usr/bin/env python3
"""
One-time backfill for stale call rows.

Calls are first synced while still 'in-progress' (duration 0). If a call
completed after its initial sync, the bulk date_created query never re-fetched
it, leaving a stale row. sync_events.py now self-heals this every run via
refetch_stale_calls(); this script applies the same repair on demand to the
full existing backlog.

Usage:
  python3 backfill_calls.py

Requires: CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY env vars.
"""

from dotenv import load_dotenv

load_dotenv()

from pull_data import get_api_key
from db import get_supabase
from sync_events import refetch_stale_calls


def main():
    api_key = get_api_key()
    sb = get_supabase()
    print("Re-fetching stale (non-terminal) calls...")
    refreshed = refetch_stale_calls(api_key, sb)
    print(f"Done. Refreshed {refreshed} calls.")


if __name__ == "__main__":
    main()
