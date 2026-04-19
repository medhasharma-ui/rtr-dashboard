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
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

BASE_URL = "https://api.close.com/api/v1"

# Close CRM IDs (REQ Pipeline)
RTR_STATUS_ID = "stat_AZ0tc4F8UzLQJyVG9vLH23R5RpMnIZdUkiNH7xvvVeb"

# All target statuses we track from RTR
TARGET_STATUSES = {
    "stat_Pn5zo8keGKa8rK4QCbg1sQAt72vREwPDPcZ9MyXv9Wf": "Active Scenario",
    "stat_ES08dw9Ij4gVsMrcuCtmviVwlJ0COaYJIrUgJtBWEtk": "Declined Scenario",
    "stat_RIXpsfGd3QDdTzdYQU16XLVaj1M6h4X6JV8qsZ8d7tW": "Addl Info Needed",
}

def get_api_key():
    key = os.environ.get("CLOSE_API_KEY")
    if not key:
        print("Error: Set CLOSE_API_KEY environment variable")
        print("  export CLOSE_API_KEY=your_api_key_here")
        sys.exit(1)
    return key


def close_get(endpoint, params=None, api_key=None):
    """Make a GET request to Close API with basic auth."""
    resp = requests.get(
        f"{BASE_URL}/{endpoint}",
        params=params,
        auth=(api_key, ""),
    )
    resp.raise_for_status()
    return resp.json()


def fetch_rtr_transitions(api_key, start_date, end_date):
    """
    Step 1: Find all RTR → target status transitions.
    Fast approach:
      a) Get all opportunities updated in date range (REQ pipeline) → unique lead_ids
      b) Per lead, query OpportunityStatusChange activities (uses lead_id + _type filter)
    """
    print(f"Fetching RTR transitions ({start_date} to {end_date})...")
    target_ids = set(TARGET_STATUSES.keys())
    pipeline_id = "pipe_5VzsEaw8Df23USMhIwmMfz"

    # Step 1a: Get unique lead_ids from recently-updated opportunities
    print("  Finding leads with recent opportunity updates...")
    lead_ids = set()
    has_more = True
    skip = 0

    while has_more:
        data = close_get(
            "opportunity/",
            params={
                "pipeline_id": pipeline_id,
                "date_updated__gte": start_date,
                "date_updated__lte": end_date,
                "_limit": 100,
                "_skip": skip,
                "_fields": "lead_id",
            },
            api_key=api_key,
        )
        for opp in data.get("data", []):
            lead_ids.add(opp["lead_id"])
        has_more = data.get("has_more", False)
        skip += 100

    print(f"  Found {len(lead_ids)} leads with updated opportunities")

    # Step 1b: For each lead, get OpportunityStatusChange activities
    transitions = []
    for i, lead_id in enumerate(lead_ids):
        if (i + 1) % 20 == 0 or i == 0:
            print(f"  Checking lead {i+1}/{len(lead_ids)} for RTR transitions...")

        data = close_get(
            "activity/",
            params={
                "lead_id": lead_id,
                "_type": "OpportunityStatusChange",
                "date_created__gte": start_date,
                "date_created__lte": end_date,
                "_limit": 100,
                "_fields": "old_status_id,new_status_id,lead_id,date_created,opportunity_id,user_id,user_name",
            },
            api_key=api_key,
        )

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

    by_type = {}
    for t in transitions:
        by_type[t["transition"]] = by_type.get(t["transition"], 0) + 1
    for label, count in by_type.items():
        print(f"  RTR → {label}: {count}")
    print(f"  Total: {len(transitions)} transitions")
    return transitions


def fetch_lead_info(api_key, lead_id):
    """Get lead name and assigned user."""
    try:
        lead = close_get(f"lead/{lead_id}/", api_key=api_key)
        display_name = lead.get("display_name", "(Unknown)")
        contacts = lead.get("contacts", [])
        contact_name = contacts[0].get("display_name", display_name) if contacts else display_name

        # Get the assigned user (opportunity owner)
        opportunities = lead.get("opportunities", [])
        user_id = None
        for opp in opportunities:
            if opp.get("user_id"):
                user_id = opp["user_id"]
                break

        return {
            "lead_name": display_name,
            "contact_name": contact_name,
            "user_id": user_id,
        }
    except Exception as e:
        print(f"  Warning: Could not fetch lead {lead_id}: {e}")
        return {"lead_name": "(Unknown)", "contact_name": "(Unknown)", "user_id": None}


def fetch_calls_for_lead(api_key, lead_id, after_timestamp):
    """
    Step 2: Find the earliest call on a lead after the trigger timestamp.
    """
    try:
        data = close_get(
            "activity/call/",
            params={
                "lead_id": lead_id,
                "date_created__gte": after_timestamp,
                "_order_by": "date_created",
                "_limit": 1,
            },
            api_key=api_key,
        )
        calls = data.get("data", [])
        if calls:
            return calls[0]["date_created"]
        return None
    except Exception as e:
        print(f"  Warning: Could not fetch calls for lead {lead_id}: {e}")
        return None


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


def classify(changed_at_str, call_at_str, now):
    """
    Step 3: Classify the lead into a bucket.
    """
    changed_at = datetime.fromisoformat(changed_at_str.replace("Z", "+00:00"))
    elapsed_mins = (now - changed_at).total_seconds() / 60

    if call_at_str:
        call_at = datetime.fromisoformat(call_at_str.replace("Z", "+00:00"))
        mins_to_call = (call_at - changed_at).total_seconds() / 60
        bucket = "within" if mins_to_call <= 120 else "after"
        return bucket, round(mins_to_call, 1)

    if elapsed_mins < 120:
        return "pending", None

    return "never", None


def main():
    parser = argparse.ArgumentParser(description="Pull Speed-to-Call data from Close CRM")
    parser.add_argument("--days", type=int, help="Pull last N days of data")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    api_key = get_api_key()
    now = datetime.now(timezone.utc)

    if args.days:
        end_date = now.strftime("%Y-%m-%d")
        start_date = (now - timedelta(days=args.days)).strftime("%Y-%m-%d")
    elif args.start and args.end:
        start_date = args.start
        end_date = args.end
    else:
        print("Specify --days N or --start/--end dates")
        sys.exit(1)

    # Step 1: Get RTR → Active Scenario transitions
    transitions = fetch_rtr_transitions(api_key, start_date, end_date)

    if not transitions:
        print("No transitions found. Check date range.")
        sys.exit(0)

    # Fetch user mapping
    print("Fetching user directory...")
    users = fetch_users(api_key)

    # Step 2 & 3: For each transition, get lead info + calls + classify
    results = []
    seen_keys = set()

    for i, t in enumerate(transitions):
        lead_id = t["lead_id"]
        changed_at = t["changed_at"]
        key = f"{lead_id}_{changed_at}"

        if key in seen_keys:
            continue
        seen_keys.add(key)

        print(f"  Processing {i+1}/{len(transitions)}: {lead_id}...")

        lead_info = fetch_lead_info(api_key, lead_id)
        call_at = fetch_calls_for_lead(api_key, lead_id, changed_at)
        bucket, mins_to_call = classify(changed_at, call_at, now)

        # Resolve AE name
        ae_user_id = lead_info.get("user_id") or t.get("user_id")
        ae_name = users.get(ae_user_id, "Unknown")

        results.append({
            "contact": lead_info["contact_name"],
            "ae": ae_name,
            "changedAt": changed_at,
            "callAt": call_at,
            "minsToCall": mins_to_call,
            "bucket": bucket,
            "leadId": lead_id,
            "opportunityId": t.get("opportunity_id"),
            "transition": t.get("transition", "Active Scenario"),
        })

    # Group by date for the dashboard
    by_date = {}
    for r in results:
        date_key = r["changedAt"][:10]
        by_date.setdefault(date_key, []).append(r)

    output = {
        "generated_at": now.isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "total_leads": len(results),
        "by_date": by_date,
        "all": results,
    }

    # Write to Supabase
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SECRET_KEY")
    if not supabase_url or not supabase_key:
        print("Error: Set SUPABASE_URL and SUPABASE_SECRET_KEY environment variables")
        sys.exit(1)

    supabase = create_client(supabase_url, supabase_key)
    supabase.table("dashboard_snapshots").insert({
        "generated_at": output["generated_at"],
        "data": output,
    }).execute()

    within = sum(1 for r in results if r["bucket"] == "within")
    after = sum(1 for r in results if r["bucket"] == "after")
    never = sum(1 for r in results if r["bucket"] == "never")
    pending = sum(1 for r in results if r["bucket"] == "pending")

    print(f"\nDone! {len(results)} leads processed.")
    print(f"  Within 2 hrs: {within}")
    print(f"  After 2 hrs:  {after}")
    print(f"  Never called: {never}")
    print(f"  Pending:      {pending}")
    print(f"\nSnapshot inserted into Supabase.")


if __name__ == "__main__":
    main()
