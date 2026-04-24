#!/usr/bin/env python3
"""
One-time full load from Close CRM into normalized Supabase tables.

Usage:
  python3 initial_load.py --days 30
  python3 initial_load.py --start 2026-03-01 --end 2026-04-24
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from pull_data import (
    get_api_key,
    close_get,
    _fetch_all_pages_parallel,
    fetch_lead_infos_parallel,
    fetch_status_changes_for_lead,
    fetch_users,
    PIPELINE_ID,
    MAX_WORKERS,
    PT,
)
from db import (
    get_supabase,
    upsert_users,
    upsert_leads,
    upsert_opportunities,
    upsert_status_changes,
    upsert_calls,
    set_sync_cursor,
)


def fetch_all_opportunities(api_key, start_date, end_date):
    """Fetch all opportunities in REQ pipeline within date range."""
    print(f"Fetching opportunities ({start_date} to {end_date})...")
    rows = _fetch_all_pages_parallel("opportunity/", {
        "pipeline_id": PIPELINE_ID,
        "date_updated__gte": start_date,
        "date_updated__lte": end_date,
        "_fields": "id,lead_id,status_id,status_label,pipeline_id,user_id,date_created,date_updated",
    }, api_key, label="opps")
    print(f"  Fetched {len(rows)} opportunities")
    return rows


def fetch_status_changes_for_leads(api_key, lead_ids, start_date, end_date):
    """Fetch status changes for all leads in parallel.

    Close API requires lead_id when filtering by _type, so we fetch per-lead
    and collect all results. Uses the full activity response (with id field)
    for upserting into the normalized table.
    """
    print(f"Fetching status changes for {len(lead_ids)} leads (parallel)...")
    all_changes = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_status_changes_full, api_key, lid, start_date, end_date): lid
            for lid in lead_ids
        }
        for future in as_completed(futures):
            try:
                all_changes.extend(future.result())
            except Exception as e:
                print(f"  Warning: status change fetch failed for {futures[future]}: {e}")
    print(f"  Fetched {len(all_changes)} status changes")
    return all_changes


def _fetch_status_changes_full(api_key, lead_id, start_date, end_date):
    """Get all OpportunityStatusChange activities for a single lead, with id field."""
    data = close_get("activity/", params={
        "lead_id": lead_id,
        "_type": "OpportunityStatusChange",
        "date_created__gte": start_date,
        "date_created__lte": end_date,
        "_limit": 100,
        "_fields": "id,old_status_id,new_status_id,lead_id,date_created,opportunity_id,user_id",
    }, api_key=api_key)
    return data.get("data", [])


def fetch_all_calls(api_key, start_date, end_date):
    """Bulk-fetch all calls in date range."""
    print(f"Fetching calls ({start_date} to {end_date})...")
    rows = _fetch_all_pages_parallel("activity/call/", {
        "date_created__gte": start_date,
        "date_created__lte": end_date,
        "_fields": "id,lead_id,user_id,date_created,duration,status",
    }, api_key, label="calls")
    print(f"  Fetched {len(rows)} calls")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Full load from Close CRM to normalized tables")
    parser.add_argument("--days", type=int, default=30, help="Pull last N days (default: 30)")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    api_key = get_api_key()
    now = datetime.now(timezone.utc)
    pt_now = now.astimezone(PT)

    if args.start and args.end:
        start_date = args.start
        end_date = args.end
        api_end = f"{args.end}T23:59:59+00:00"
    else:
        end_date = pt_now.strftime("%Y-%m-%d")
        start_date = (pt_now - timedelta(days=args.days)).strftime("%Y-%m-%d")
        api_end = now.isoformat()

    print(f"=== Initial Load: {start_date} to {end_date} ===\n")
    sb = get_supabase()
    t_total = time.time()

    # Step 1: Fetch users
    t0 = time.time()
    print("Step 1: Fetching users...")
    raw_users = close_get("user/", api_key=api_key)
    user_list = [
        {
            "id": u["id"],
            "name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
            "email": u.get("email"),
        }
        for u in raw_users.get("data", [])
    ]
    count = upsert_users(sb, user_list)
    print(f"  Upserted {count} users ({time.time()-t0:.1f}s)\n")

    # Step 2: Fetch opportunities and calls in parallel (status changes need lead_ids first)
    print("Step 2: Fetching opportunities and calls in parallel...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_opps = executor.submit(fetch_all_opportunities, api_key, start_date, api_end)
        f_calls = executor.submit(fetch_all_calls, api_key, start_date, api_end)

        raw_opps = f_opps.result()
        raw_calls = f_calls.result()
    print(f"  Parallel fetch done ({time.time()-t0:.1f}s)\n")

    # Upsert opportunities
    t0 = time.time()
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
    count = upsert_opportunities(sb, opp_rows)
    print(f"  Upserted {count} opportunities ({time.time()-t0:.1f}s)")

    # Upsert calls
    t0 = time.time()
    count = upsert_calls(sb, raw_calls)
    print(f"  Upserted {count} calls ({time.time()-t0:.1f}s)\n")

    # Step 3: Get lead_ids from opportunities, then fetch status changes per lead
    lead_ids = list(set(o.get("lead_id") for o in raw_opps if o.get("lead_id")))
    print(f"Step 3: Fetching status changes for {len(lead_ids)} leads...")
    t0 = time.time()
    raw_changes = fetch_status_changes_for_leads(api_key, lead_ids, start_date, api_end)
    count = upsert_status_changes(sb, raw_changes)
    print(f"  Upserted {count} status changes ({time.time()-t0:.1f}s)\n")

    # Step 4: Fetch lead info for all unique leads
    print("Step 4: Fetching lead info...")
    t0 = time.time()
    all_lead_ids = set(lead_ids)
    for c in raw_changes:
        if c.get("lead_id"):
            all_lead_ids.add(c["lead_id"])
    all_lead_ids = list(all_lead_ids)
    print(f"  {len(all_lead_ids)} unique leads to fetch")

    lead_infos = fetch_lead_infos_parallel(api_key, all_lead_ids)
    lead_rows = [
        {
            "id": lid,
            "display_name": info.get("lead_name"),
            "contact_name": info.get("contact_name"),
        }
        for lid, info in lead_infos.items()
    ]
    count = upsert_leads(sb, lead_rows)
    print(f"  Upserted {count} leads ({time.time()-t0:.1f}s)\n")

    # Step 5: Set sync cursor
    set_sync_cursor(sb, entity_type="event_log", last_event_date=now.isoformat())
    print(f"  Sync cursor set to {now.isoformat()}")

    elapsed = round(time.time() - t_total, 1)
    print(f"\n=== Initial load complete in {elapsed}s ===")
    print(f"  Opportunities: {len(raw_opps)}")
    print(f"  Status changes: {len(raw_changes)}")
    print(f"  Calls: {len(raw_calls)}")
    print(f"  Leads: {len(lead_rows)}")
    print(f"  Users: {len(user_list)}")


if __name__ == "__main__":
    main()
