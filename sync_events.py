#!/usr/bin/env python3
"""
Incremental sync from Close CRM into normalized Supabase tables.

Fetches only data created/updated since the last sync:
- Opportunities (to discover new lead_ids)
- Status changes (per-lead, parallel)
- Calls (bulk via activity/call/ endpoint)
- New leads (only those not already in DB)

Usage:
  python3 sync_events.py

Requires: CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY env vars.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from pull_data import (
    get_api_key,
    close_get,
    _fetch_all_pages_parallel,
    fetch_lead_infos_parallel,
    PIPELINE_ID,
    MAX_WORKERS,
)
from db import (
    get_supabase,
    upsert_leads,
    upsert_opportunities,
    upsert_status_changes,
    upsert_calls,
    get_sync_cursor,
    set_sync_cursor,
)


def fetch_recent_opportunities(api_key, since_date):
    """Fetch opportunities updated since last sync."""
    rows = _fetch_all_pages_parallel("opportunity/", {
        "pipeline_id": PIPELINE_ID,
        "date_updated__gte": since_date,
        "_fields": "id,lead_id,status_id,status_label,pipeline_id,user_id,date_created,date_updated",
    }, api_key, label="opps")
    return rows


def fetch_status_changes_for_leads(api_key, lead_ids, since_date):
    """Fetch status changes for leads in parallel."""
    all_changes = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_status_changes, api_key, lid, since_date): lid
            for lid in lead_ids
        }
        for future in as_completed(futures):
            try:
                all_changes.extend(future.result())
            except Exception as e:
                print(f"  Warning: failed for {futures[future]}: {e}")
    return all_changes


def _fetch_status_changes(api_key, lead_id, since_date):
    """Get status changes for a single lead since a date."""
    data = close_get("activity/", params={
        "lead_id": lead_id,
        "_type": "OpportunityStatusChange",
        "date_created__gte": since_date,
        "_limit": 100,
        "_fields": "id,old_status_id,new_status_id,lead_id,date_created,opportunity_id,user_id",
    }, api_key=api_key)
    return data.get("data", [])


def fetch_recent_calls(api_key, since_date):
    """Fetch calls created since last sync via bulk endpoint."""
    rows = _fetch_all_pages_parallel("activity/call/", {
        "date_created__gte": since_date,
        "_fields": "id,lead_id,user_id,date_created,duration,status",
    }, api_key, label="calls")
    return rows


def fetch_calls_paginated(api_key, since_date, skip_from=0, max_pages=10):
    """Fetch calls in parallel page chunks. Returns (rows, done, next_skip).

    Fires up to `max_pages` pages in parallel starting from `skip_from`.
    Returns early when a page has < 100 rows (end of data).
    Used by api/sync.py to stay within the ~5s time budget per step.
    """
    base_params = {
        "date_created__gte": since_date,
        "_fields": "id,lead_id,user_id,date_created,duration,status",
        "_limit": 100,
    }
    skips = [skip_from + i * 100 for i in range(max_pages)]
    page_results = {}

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(close_get, "activity/call/",
                            {**base_params, "_skip": skip}, api_key): skip
            for skip in skips
        }
        for future in as_completed(futures):
            skip = futures[future]
            try:
                data = future.result()
                page_results[skip] = data.get("data", [])
            except Exception:
                page_results[skip] = []

    rows = []
    done = False
    for skip in skips:
        page_rows = page_results.get(skip, [])
        rows.extend(page_rows)
        if len(page_rows) < 100:
            done = True
            break

    next_skip = skip_from + max_pages * 100
    print(f"  [calls_paginated] skip={skip_from} pages={max_pages} → {len(rows)} rows, done={done}")
    return rows, done, next_skip


def run_sync(api_key=None):
    """Run incremental sync. Returns a result dict.

    Can be called from CLI (main) or from the api/sync.py endpoint.
    """
    if not api_key:
        api_key = get_api_key()
    sb = get_supabase()
    now = datetime.now(timezone.utc)

    # Read sync cursor
    cursor = get_sync_cursor(sb, entity_type="event_log")
    if not cursor:
        return {"error": "No sync cursor found. Run initial_load.py first."}

    last_sync = cursor["last_event_date"]
    print(f"=== Incremental Sync ===")
    print(f"  Last sync: {last_sync}")
    print(f"  Now:       {now.isoformat()}")

    t_total = time.time()

    # 1. Fetch recently-updated opportunities to find lead_ids with activity
    print("\n1. Fetching recent opportunities...")
    t0 = time.time()
    raw_opps = fetch_recent_opportunities(api_key, last_sync)
    lead_ids = list(set(o.get("lead_id") for o in raw_opps if o.get("lead_id")))
    print(f"  {len(raw_opps)} opportunities, {len(lead_ids)} unique leads ({time.time()-t0:.1f}s)")

    # Upsert opportunities
    if raw_opps:
        opp_rows = [
            {
                "id": o["id"],
                "lead_id": o.get("lead_id"),
                "status_id": o.get("status_id"),
                "status_label": o.get("status_label"),
                "pipeline_id": o.get("pipeline_id"),
                "user_id": o.get("user_id"),
                "created_at": o.get("date_created"),
                "updated_at": o.get("date_updated"),
            }
            for o in raw_opps
        ]
        upsert_opportunities(sb, opp_rows)

    # 2. Fetch status changes and calls in parallel
    print("\n2. Fetching status changes and calls...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_changes = executor.submit(fetch_status_changes_for_leads, api_key, lead_ids, last_sync) if lead_ids else None
        f_calls = executor.submit(fetch_recent_calls, api_key, last_sync)

        changes = f_changes.result() if f_changes else []
        call_rows = f_calls.result()
    print(f"  {len(changes)} status changes, {len(call_rows)} calls ({time.time()-t0:.1f}s)")

    # Upsert
    if changes:
        upsert_status_changes(sb, changes)
    if call_rows:
        upsert_calls(sb, call_rows)

    # 3. Check for new leads
    print("\n3. Checking for new leads...")
    t0 = time.time()
    all_lead_ids = set(lead_ids)
    for c in changes:
        if c.get("lead_id"):
            all_lead_ids.add(c["lead_id"])
    for c in call_rows:
        if c.get("lead_id"):
            all_lead_ids.add(c["lead_id"])

    missing = []
    if all_lead_ids:
        existing_ids = set()
        for i in range(0, len(list(all_lead_ids)), 100):
            chunk = list(all_lead_ids)[i : i + 100]
            rows = sb.table("leads").select("id").in_("id", chunk).execute()
            existing_ids.update(r["id"] for r in rows.data)
        missing = [lid for lid in all_lead_ids if lid not in existing_ids]

    if missing:
        print(f"  {len(missing)} new leads to fetch")
        lead_infos = fetch_lead_infos_parallel(api_key, missing)
        lead_rows = [
            {
                "id": lid,
                "display_name": info.get("lead_name"),
                "contact_name": info.get("contact_name"),
            }
            for lid, info in lead_infos.items()
        ]
        upsert_leads(sb, lead_rows)
        print(f"  Upserted {len(lead_rows)} leads ({time.time()-t0:.1f}s)")
    else:
        print(f"  No new leads ({time.time()-t0:.1f}s)")

    # 4. Update sync cursor
    set_sync_cursor(sb, entity_type="event_log", last_event_date=now.isoformat())

    elapsed = round(time.time() - t_total, 1)
    print(f"\n=== Sync complete in {elapsed}s ===")

    return {
        "status": "ok",
        "elapsed_s": elapsed,
        "last_sync": last_sync,
        "new_cursor": now.isoformat(),
        "opportunities": len(raw_opps),
        "status_changes": len(changes),
        "calls": len(call_rows),
        "new_leads": len(missing),
    }


def main():
    result = run_sync()
    if result.get("error"):
        print(f"Error: {result['error']}")
        sys.exit(1)
    print(f"  Opportunities: {result['opportunities']}")
    print(f"  Status changes: {result['status_changes']}")
    print(f"  Calls: {result['calls']}")
    print(f"  New leads: {result['new_leads']}")
    print(f"  Cursor: {result['new_cursor']}")


if __name__ == "__main__":
    main()
