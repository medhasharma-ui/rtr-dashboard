#!/usr/bin/env python3
"""
Incremental sync via Close Event Log API.

Reads the last sync cursor from Supabase, fetches new/changed events since
that timestamp, and upserts them into normalized tables.

Usage:
  python3 sync_events.py

Requires: CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY env vars.
"""

import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from pull_data import (
    get_api_key,
    close_get,
    fetch_lead_infos_parallel,
)
from db import (
    get_supabase,
    upsert_leads,
    upsert_status_changes,
    upsert_calls,
    get_sync_cursor,
    set_sync_cursor,
)

EVENT_LOG_RETENTION_DAYS = 30
EVENT_PAGE_LIMIT = 50


def fetch_events(api_key, object_type, date_updated_gte):
    """Fetch all events of a given object_type since date_updated_gte.

    Uses cursor-based pagination via the Close Event Log API.
    Returns list of event dicts.
    """
    all_events = []
    params = {
        "object_type": object_type,
        "date_updated__gte": date_updated_gte,
        "_limit": EVENT_PAGE_LIMIT,
    }
    cursor_next = None
    page = 0

    while True:
        if cursor_next:
            params["cursor"] = cursor_next

        data = close_get("event/", params=params, api_key=api_key)
        events = data.get("data", [])
        all_events.extend(events)
        page += 1

        cursor_next = data.get("cursor_next")
        if not cursor_next or not events:
            break

        print(f"  [{object_type}] page {page}: {len(events)} events (total so far: {len(all_events)})")

    print(f"  [{object_type}] done: {len(all_events)} events in {page} pages")
    return all_events


def extract_status_changes(events):
    """Extract status change data from event log entries."""
    changes = []
    for event in events:
        obj = event.get("data", {})
        if not obj or not obj.get("id"):
            continue
        changes.append({
            "id": obj["id"],
            "lead_id": obj.get("lead_id"),
            "opportunity_id": obj.get("opportunity_id"),
            "old_status_id": obj.get("old_status_id"),
            "new_status_id": obj.get("new_status_id"),
            "date_created": obj.get("date_created"),
            "user_id": obj.get("user_id"),
        })
    return changes


def extract_calls(events):
    """Extract call data from event log entries."""
    calls = []
    for event in events:
        obj = event.get("data", {})
        if not obj or not obj.get("id"):
            continue
        calls.append({
            "id": obj["id"],
            "lead_id": obj.get("lead_id"),
            "user_id": obj.get("user_id"),
            "date_created": obj.get("date_created"),
            "duration": obj.get("duration", 0),
            "status": obj.get("status"),
        })
    return calls


def get_latest_event_date(events):
    """Get the latest date_updated from a list of events."""
    latest = None
    for event in events:
        date_updated = event.get("date_updated")
        if date_updated:
            if latest is None or date_updated > latest:
                latest = date_updated
    return latest


def main():
    api_key = get_api_key()
    sb = get_supabase()
    now = datetime.now(timezone.utc)

    # Read sync cursor
    cursor = get_sync_cursor(sb, entity_type="event_log")
    if not cursor:
        print("Error: No sync cursor found. Run initial_load.py first.")
        sys.exit(1)

    last_event_date = cursor["last_event_date"]
    print(f"=== Incremental Sync ===")
    print(f"  Last sync: {last_event_date}")

    # Check retention limit
    last_dt = datetime.fromisoformat(last_event_date.replace("Z", "+00:00"))
    days_since = (now - last_dt).days
    if days_since > EVENT_LOG_RETENTION_DAYS:
        print(f"\nWARNING: Last sync was {days_since} days ago.")
        print(f"Close Event Log only retains {EVENT_LOG_RETENTION_DAYS} days of data.")
        print("Consider re-running: python3 initial_load.py --days 30")
        print("Continuing anyway — some data may be missed.\n")

    t_total = time.time()
    latest_dates = []

    # Fetch status change events
    print("\nFetching status change events...")
    t0 = time.time()
    sc_events = fetch_events(api_key, "activity.opportunity_status_change", last_event_date)
    changes = extract_status_changes(sc_events)
    if changes:
        count = upsert_status_changes(sb, changes)
        print(f"  Upserted {count} status changes ({time.time()-t0:.1f}s)")
    else:
        print(f"  No new status changes ({time.time()-t0:.1f}s)")
    sc_latest = get_latest_event_date(sc_events)
    if sc_latest:
        latest_dates.append(sc_latest)

    # Fetch call events
    print("\nFetching call events...")
    t0 = time.time()
    call_events = fetch_events(api_key, "activity.call", last_event_date)
    calls = extract_calls(call_events)
    if calls:
        count = upsert_calls(sb, calls)
        print(f"  Upserted {count} calls ({time.time()-t0:.1f}s)")
    else:
        print(f"  No new calls ({time.time()-t0:.1f}s)")
    call_latest = get_latest_event_date(call_events)
    if call_latest:
        latest_dates.append(call_latest)

    # Check for new leads not yet in the leads table
    print("\nChecking for new leads...")
    t0 = time.time()
    new_lead_ids = set()
    for c in changes:
        if c.get("lead_id"):
            new_lead_ids.add(c["lead_id"])
    for c in calls:
        if c.get("lead_id"):
            new_lead_ids.add(c["lead_id"])

    if new_lead_ids:
        # Check which leads already exist
        existing_ids = set()
        new_lead_list = list(new_lead_ids)
        for i in range(0, len(new_lead_list), 100):
            chunk = new_lead_list[i : i + 100]
            rows = sb.table("leads").select("id").in_("id", chunk).execute()
            existing_ids.update(r["id"] for r in rows.data)

        missing = [lid for lid in new_lead_ids if lid not in existing_ids]
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
            print(f"  All {len(new_lead_ids)} leads already in DB ({time.time()-t0:.1f}s)")
    else:
        print(f"  No leads to check ({time.time()-t0:.1f}s)")

    # Update sync cursor
    if latest_dates:
        new_cursor_date = max(latest_dates)
    else:
        new_cursor_date = now.isoformat()
    set_sync_cursor(sb, entity_type="event_log", last_event_date=new_cursor_date)

    elapsed = round(time.time() - t_total, 1)
    print(f"\n=== Sync complete in {elapsed}s ===")
    print(f"  Status changes: {len(changes)}")
    print(f"  Calls: {len(calls)}")
    print(f"  New cursor: {new_cursor_date}")


if __name__ == "__main__":
    main()
