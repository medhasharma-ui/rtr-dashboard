#!/usr/bin/env python3
"""
Speed-to-Call Data Pull
Fetches RTR → Active Scenario transitions from Close CRM and matches them
with post-trigger call activity to classify each lead.

Usage:
  export CLOSE_API_KEY=your_key_here
  python3 pull_data.py --days 7
  python3 pull_data.py --start 2026-04-01 --end 2026-04-15
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

BASE_URL = "https://api.close.com/api/v1"

# FlyHomes' business day runs on Pacific time
PT = ZoneInfo("America/Los_Angeles")

# Close CRM IDs (REQ Pipeline)
PIPELINE_ID = "pipe_5VzsEaw8Df23USMhIwmMfz"
RTR_STATUS_ID = "stat_AZ0tc4F8UzLQJyVG9vLH23R5RpMnIZdUkiNH7xvvVeb"

TARGET_STATUSES = {
    "stat_Pn5zo8keGKa8rK4QCbg1sQAt72vREwPDPcZ9MyXv9Wf": "Active Scenario",
    "stat_ES08dw9Ij4gVsMrcuCtmviVwlJ0COaYJIrUgJtBWEtk": "Declined Scenario",
    "stat_RIXpsfGd3QDdTzdYQU16XLVaj1M6h4X6JV8qsZ8d7tW": "Addl Info Needed",
}

MAX_WORKERS = 10


def get_api_key():
    key = os.environ.get("CLOSE_API_KEY")
    if not key:
        print("Error: Set CLOSE_API_KEY environment variable")
        sys.exit(1)
    return key


def close_get(endpoint, params=None, api_key=None):
    """Make a GET request to Close API with basic auth and retry on 429."""
    for attempt in range(3):
        resp = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=params,
            auth=(api_key, ""),
        )
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 1))
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


# ── Step 1a: Get lead IDs from updated opportunities ──

def _fetch_page(endpoint, base_params, skip, api_key):
    """Fetch a single paginated page at the given skip offset."""
    params = {**base_params, "_skip": skip}
    return close_get(endpoint, params=params, api_key=api_key)


def _fetch_all_pages_parallel(endpoint, base_params, api_key, label="items"):
    """Fetch page 0 to learn total, then fire remaining pages in parallel.
    When total_results is missing, uses speculative parallel batches."""
    base_params = {**base_params, "_limit": 100}

    # Page 0 — learn how many results exist
    t0 = time.time()
    first = close_get(endpoint, params={**base_params, "_skip": 0}, api_key=api_key)
    rows = list(first.get("data", []))
    total_results = first.get("total_results", 0)
    has_more = first.get("has_more", False)
    print(f"  [{label}] page 0: {len(rows)} rows, total_results={total_results}, has_more={has_more} ({time.time()-t0:.1f}s)")

    if not has_more:
        return rows

    if total_results > len(rows):
        # We know the total — fire all remaining pages in parallel
        remaining_skips = list(range(100, total_results, 100))
        print(f"  [{label}] parallel fetch: {len(remaining_skips)} remaining pages")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(_fetch_page, endpoint, base_params, skip, api_key): skip
                for skip in remaining_skips
            }
            for future in as_completed(futures):
                try:
                    data = future.result()
                    rows.extend(data.get("data", []))
                except Exception as e:
                    print(f"  Warning: page fetch failed (skip={futures[future]}): {e}")
        print(f"  [{label}] parallel done: {len(rows)} total rows ({time.time()-t0:.1f}s)")
    else:
        # No total_results — speculative parallel: fire many pages, collect until data runs out
        print(f"  [{label}] speculative parallel (no total_results)")
        t0 = time.time()
        SPEC_PAGES = 50  # speculate up to 5000 items
        spec_skips = list(range(100, 100 + SPEC_PAGES * 100, 100))
        page_results = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(_fetch_page, endpoint, base_params, skip, api_key): skip
                for skip in spec_skips
            }
            for future in as_completed(futures):
                skip = futures[future]
                try:
                    data = future.result()
                    page_results[skip] = data.get("data", [])
                except Exception:
                    page_results[skip] = []
        # Collect in order, stop when a page has < 100 rows (partial/empty = end of data)
        all_full = True
        for skip in spec_skips:
            page_rows = page_results.get(skip, [])
            rows.extend(page_rows)
            if len(page_rows) < 100:
                all_full = False
                break
        # If every speculative page was full, there may be more data — continue sequentially
        if all_full:
            print(f"  [{label}] speculative pages exhausted, continuing sequentially...")
            next_skip = spec_skips[-1] + 100
            while True:
                data = close_get(endpoint, params={**base_params, "_skip": next_skip}, api_key=api_key)
                page_rows = data.get("data", [])
                rows.extend(page_rows)
                if len(page_rows) < 100:
                    break
                next_skip += 100
        print(f"  [{label}] speculative done: {len(rows)} total rows ({time.time()-t0:.1f}s)")

    return rows


def fetch_lead_ids(api_key, start_date, end_date):
    """Get unique lead_ids from recently-updated opportunities in REQ pipeline."""
    print(f"Fetching lead IDs ({start_date} to {end_date})...")
    rows = _fetch_all_pages_parallel("opportunity/", {
        "pipeline_id": PIPELINE_ID,
        "date_updated__gte": start_date,
        "date_updated__lte": end_date,
        "_fields": "lead_id",
    }, api_key, label="opps")
    lead_ids = list(set(opp["lead_id"] for opp in rows))
    print(f"  Found {len(lead_ids)} leads with updated opportunities")
    return lead_ids


# ── Step 1b: Get status changes per lead (parallel) ──

def fetch_status_changes_for_lead(api_key, lead_id, start_date, end_date):
    """Get RTR → target status transitions for a single lead."""
    target_ids = set(TARGET_STATUSES.keys())
    transitions = []
    data = close_get("activity/", params={
        "lead_id": lead_id,
        "_type": "OpportunityStatusChange",
        "date_created__gte": start_date,
        "date_created__lte": end_date,
        "_limit": 100,
        "_fields": "old_status_id,new_status_id,lead_id,date_created,opportunity_id,user_id,user_name",
    }, api_key=api_key)
    for activity in data.get("data", []):
        old_id = activity.get("old_status_id")
        new_id = activity.get("new_status_id")
        if old_id == RTR_STATUS_ID and new_id in target_ids:
            transitions.append({
                "lead_id": activity["lead_id"],
                "opportunity_id": activity.get("opportunity_id"),
                "changed_at": activity["date_created"],
                "user_id": activity.get("user_id"),
                "transition": TARGET_STATUSES[new_id],
            })
    return transitions


def fetch_transitions_parallel(api_key, lead_ids, start_date, end_date):
    """Fetch RTR transitions for all leads in parallel."""
    print(f"  Fetching status changes for {len(lead_ids)} leads (parallel)...")
    all_transitions = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_status_changes_for_lead, api_key, lid, start_date, end_date): lid
            for lid in lead_ids
        }
        for future in as_completed(futures):
            try:
                all_transitions.extend(future.result())
            except Exception as e:
                print(f"  Warning: status change fetch failed for {futures[future]}: {e}")

    by_type = {}
    for t in all_transitions:
        by_type[t["transition"]] = by_type.get(t["transition"], 0) + 1
    for label, count in by_type.items():
        print(f"  RTR → {label}: {count}")
    print(f"  Total: {len(all_transitions)} transitions")
    return all_transitions


# ── Step 2: Bulk-fetch all calls ──

def fetch_all_calls_bulk(api_key, start_date, end_date):
    """Bulk-fetch all calls in date range. Returns dict of lead_id → sorted list of call dicts."""
    print("Bulk-fetching all calls...")
    rows = _fetch_all_pages_parallel("activity/call/", {
        "date_created__gte": start_date,
        "date_created__lte": end_date,
        "_order_by": "date_created",
        "_fields": "lead_id,date_created,duration,status",
    }, api_key, label="calls")
    return _calls_rows_to_dict(rows)


def _calls_rows_to_dict(rows):
    """Convert raw call rows to {lead_id: [{ts, dur, st}]} dict."""
    all_calls = {}
    for call in rows:
        lid = call["lead_id"]
        all_calls.setdefault(lid, []).append({
            "ts": call["date_created"],
            "dur": call.get("duration", 0),
            "st": call.get("status", ""),
        })
    print(f"  Cached {len(rows)} calls across {len(all_calls)} leads")
    return all_calls


def fetch_calls_chunk(api_key, start_date, end_date, skip_from=0, max_pages=25):
    """Fetch a chunk of call pages speculatively. Returns (rows, done).
    `done` is True when a page returned < 100 rows (end of data)."""
    base_params = {
        "date_created__gte": start_date,
        "date_created__lte": end_date,
        "_order_by": "date_created",
        "_fields": "lead_id,date_created,duration,status",
        "_limit": 100,
    }
    spec_skips = [skip_from + i * 100 for i in range(max_pages)]
    page_results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(_fetch_page, "activity/call/", base_params, skip, api_key): skip
            for skip in spec_skips
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
    for skip in spec_skips:
        page_rows = page_results.get(skip, [])
        rows.extend(page_rows)
        if len(page_rows) < 100:
            done = True
            break

    print(f"  [calls_chunk] skip_from={skip_from} max_pages={max_pages} → {len(rows)} rows, done={done}")
    return rows, done


def find_earliest_call(bulk_calls, lead_id, after_timestamp):
    """Find the earliest call for a lead after a given timestamp using bulk data."""
    calls = bulk_calls.get(lead_id, [])
    if not calls:
        return None
    after_dt = datetime.fromisoformat(after_timestamp.replace("Z", "+00:00"))
    for c in sorted(calls, key=lambda x: x["ts"]):
        call_dt = datetime.fromisoformat(c["ts"].replace("Z", "+00:00"))
        if call_dt >= after_dt:
            return c["ts"]
    return None


def find_pre_trigger_call(bulk_calls, lead_id, changed_at_str):
    """Find a connected call in the 30-min window before the status change, using bulk data.
    Connected = duration > 0 AND status == 'completed' (Close's 'answered')."""
    calls = bulk_calls.get(lead_id, [])
    if not calls:
        return None
    changed_at = datetime.fromisoformat(changed_at_str.replace("Z", "+00:00"))
    window_start = changed_at - timedelta(minutes=30)
    for c in sorted(calls, key=lambda x: x["ts"]):
        call_dt = datetime.fromisoformat(c["ts"].replace("Z", "+00:00"))
        if call_dt >= changed_at:
            continue
        if call_dt >= window_start and c.get("dur", 0) > 0 and c.get("st") == "completed":
            return c["ts"]
    return None


# ── Step 3: Lead info (parallel with _fields) ──

def fetch_lead_info(api_key, lead_id):
    """Get lead name and assigned user."""
    try:
        lead = close_get(
            f"lead/{lead_id}/",
            params={"_fields": "display_name,contacts,opportunities"},
            api_key=api_key,
        )
        display_name = lead.get("display_name", "(Unknown)")
        contacts = lead.get("contacts", [])
        contact_name = contacts[0].get("display_name", display_name) if contacts else display_name
        opportunities = lead.get("opportunities", [])
        user_id = None
        for opp in opportunities:
            if opp.get("user_id"):
                user_id = opp["user_id"]
                break
        return {
            "lead_id": lead_id,
            "lead_name": display_name,
            "contact_name": contact_name,
            "user_id": user_id,
        }
    except Exception as e:
        print(f"  Warning: Could not fetch lead {lead_id}: {e}")
        return {"lead_id": lead_id, "lead_name": "(Unknown)", "contact_name": "(Unknown)", "user_id": None}


def fetch_lead_infos_parallel(api_key, lead_ids):
    """Fetch lead info for multiple leads in parallel."""
    print(f"  Fetching lead info for {len(lead_ids)} leads (parallel)...")
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_lead_info, api_key, lid): lid
            for lid in lead_ids
        }
        for future in as_completed(futures):
            info = future.result()
            results[info["lead_id"]] = info
    return results


# ── Step 4: Users ──

def fetch_users(api_key):
    """Get user ID → name mapping."""
    data = close_get("user/", api_key=api_key)
    users = {}
    for user in data.get("data", []):
        uid = user["id"]
        first = user.get("first_name", "")
        last = user.get("last_name", "")
        users[uid] = f"{first} {last}".strip()
    return users


# ── Step 5: Classify ──

def classify(changed_at_str, call_at_str, pre_call_at_str, now):
    """
    Classify a lead into a bucket.
    A connected call in the 30-min pre-trigger window also counts as "within".
    """
    changed_at = datetime.fromisoformat(changed_at_str.replace("Z", "+00:00"))
    elapsed_mins = (now - changed_at).total_seconds() / 60
    has_pre = bool(pre_call_at_str)

    if call_at_str:
        call_at = datetime.fromisoformat(call_at_str.replace("Z", "+00:00"))
        mins_to_call = (call_at - changed_at).total_seconds() / 60
        bucket = "within" if (has_pre or mins_to_call <= 120) else "after"
        return bucket, round(mins_to_call, 1)

    if has_pre:
        return "within", None

    if elapsed_mins < 120:
        return "pending", None
    return "never", None


# ── Processing helpers ──

def process_transitions(transitions, bulk_calls, lead_infos, users, now):
    """Classify transitions using pre-fetched bulk data."""
    results = []
    seen_keys = set()
    for t in transitions:
        lead_id = t["lead_id"]
        changed_at = t["changed_at"]
        key = f"{lead_id}_{changed_at}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        lead_info = lead_infos.get(lead_id, {
            "lead_name": "(Unknown)", "contact_name": "(Unknown)", "user_id": None
        })
        call_at = find_earliest_call(bulk_calls, lead_id, changed_at)
        pre_call_at = find_pre_trigger_call(bulk_calls, lead_id, changed_at)
        bucket, mins_to_call = classify(changed_at, call_at, pre_call_at, now)

        ae_user_id = lead_info.get("user_id") or t.get("user_id")
        ae_name = users.get(ae_user_id, "Unknown")

        results.append({
            "contact": lead_info.get("contact_name", "(Unknown)"),
            "ae": ae_name,
            "changedAt": changed_at,
            "callAt": call_at,
            "preCallAt": pre_call_at,
            "preCall": bool(pre_call_at),
            "minsToCall": mins_to_call,
            "bucket": bucket,
            "leadId": lead_id,
            "opportunityId": t.get("opportunity_id"),
            "transition": t.get("transition", "Active Scenario"),
        })
    return results


def build_snapshot(results, start_date, end_date, now, range_type="custom"):
    """Build the final snapshot JSON."""
    by_date = {}
    for r in results:
        changed_at_utc = datetime.fromisoformat(r["changedAt"].replace("Z", "+00:00"))
        date_key = changed_at_utc.astimezone(PT).strftime("%Y-%m-%d")
        by_date.setdefault(date_key, []).append(r)
    return {
        "generated_at": now.isoformat(),
        "range_type": range_type,
        "start_date": start_date,
        "end_date": end_date,
        "total_leads": len(results),
        "by_date": by_date,
        "all": results,
    }


# ── CLI entry point ──

def main():
    parser = argparse.ArgumentParser(description="Pull Speed-to-Call data from Close CRM")
    parser.add_argument("--days", type=int, help="Pull last N days of data")
    parser.add_argument("--mtd", action="store_true", help="Pull from the 1st of the current PT month through now")
    parser.add_argument("--recent", action="store_true", help="Pull today + yesterday (PT) only")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    api_key = get_api_key()
    now = datetime.now(timezone.utc)
    pt_now = now.astimezone(PT)

    if args.mtd:
        range_type = "mtd"
        end_date = pt_now.strftime("%Y-%m-%d")
        start_date = pt_now.replace(day=1).strftime("%Y-%m-%d")
        api_end = now.isoformat()
    elif args.recent:
        range_type = "recent"
        end_date = pt_now.strftime("%Y-%m-%d")
        start_date = (pt_now - timedelta(days=1)).strftime("%Y-%m-%d")
        api_end = now.isoformat()
    elif args.days:
        range_type = "custom"
        end_date = pt_now.strftime("%Y-%m-%d")
        start_date = (pt_now - timedelta(days=args.days)).strftime("%Y-%m-%d")
        api_end = now.isoformat()
    elif args.start and args.end:
        range_type = "custom"
        start_date = args.start
        end_date = args.end
        api_end = f"{args.end}T23:59:59+00:00"
    else:
        print("Specify --mtd, --recent, --days N, or --start/--end dates")
        sys.exit(1)

    # Step 1: Get lead IDs
    lead_ids = fetch_lead_ids(api_key, start_date, api_end)
    if not lead_ids:
        print("No leads found. Check date range.")
        sys.exit(0)

    # Step 2: Transitions, bulk calls, and users — all in parallel
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_transitions = executor.submit(fetch_transitions_parallel, api_key, lead_ids, start_date, api_end)
        f_calls = executor.submit(fetch_all_calls_bulk, api_key, start_date, api_end)
        f_users = executor.submit(fetch_users, api_key)

        transitions = f_transitions.result()
        bulk_calls = f_calls.result()
        users = f_users.result()

    if not transitions:
        print("No RTR transitions found.")
        sys.exit(0)

    # Step 3: Lead info for transition leads (parallel)
    transition_lead_ids = list(set(t["lead_id"] for t in transitions))
    lead_infos = fetch_lead_infos_parallel(api_key, transition_lead_ids)

    # Step 4: Classify
    results = process_transitions(transitions, bulk_calls, lead_infos, users, now)

    # Step 5: Build snapshot and write to Supabase
    snapshot = build_snapshot(results, start_date, end_date, now, range_type)

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SECRET_KEY")
    if not supabase_url or not supabase_key:
        print("Error: Set SUPABASE_URL and SUPABASE_SECRET_KEY environment variables")
        sys.exit(1)

    supabase = create_client(supabase_url, supabase_key)
    supabase.table("dashboard_snapshots").insert({
        "generated_at": snapshot["generated_at"],
        "data": snapshot,
    }).execute()

    within = sum(1 for r in results if r["bucket"] == "within")
    after = sum(1 for r in results if r["bucket"] == "after")
    never = sum(1 for r in results if r["bucket"] == "never")
    pending = sum(1 for r in results if r["bucket"] == "pending")
    pre_call = sum(1 for r in results if r.get("preCall"))

    print(f"\nDone! {len(results)} leads processed.")
    print(f"  Within 2 hrs: {within}")
    print(f"  Pre-call:     {pre_call}")
    print(f"  After 2 hrs:  {after}")
    print(f"  Never called: {never}")
    print(f"  Pending:      {pending}")
    print(f"\nSnapshot inserted into Supabase.")


if __name__ == "__main__":
    main()
