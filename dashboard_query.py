"""
Shared dashboard query logic for relational tables.

Used by both api/dashboard.py and api/snapshot.py to compute dashboard JSON
from normalized Supabase tables.
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from supabase import create_client

from pull_data import (
    classify,
    build_snapshot,
    find_earliest_call,
    find_pre_trigger_call,
    RTR_STATUS_ID,
    TARGET_STATUSES,
)

PT = ZoneInfo("America/Los_Angeles")

QUERY_CHUNK = 100


def _get_supabase():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )


def _chunked_in_query(sb, table, column, ids, select="*"):
    """Query with .in_() in chunks to avoid PostgREST limits."""
    all_rows = []
    id_list = list(ids)
    for i in range(0, len(id_list), QUERY_CHUNK):
        chunk = id_list[i : i + QUERY_CHUNK]
        rows = sb.table(table).select(select).in_(column, chunk).execute()
        all_rows.extend(rows.data)
    return all_rows


def query_dashboard(start_date, end_date, range_type="custom"):
    """Query relational tables and compute dashboard buckets.

    start_date/end_date are PT date strings (YYYY-MM-DD). We convert to UTC
    bounds so that records landing on e.g. April 23 PT (which may be April 24
    UTC) are included correctly.

    Returns the same JSON shape as build_snapshot().
    """
    sb = _get_supabase()
    now = datetime.now(timezone.utc)

    target_status_ids = list(TARGET_STATUSES.keys())

    # Convert PT date range → UTC bounds
    # Start of start_date in PT → UTC
    start_pt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=PT)
    start_utc = start_pt.astimezone(timezone.utc).isoformat()
    # End of end_date in PT (23:59:59) → UTC
    end_pt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=PT
    )
    end_utc = end_pt.astimezone(timezone.utc).isoformat()

    # 1. Query opportunity_status_changes: RTR ��� target statuses in date range
    query = (
        sb.table("opportunity_status_changes")
        .select("*")
        .eq("old_status_id", RTR_STATUS_ID)
        .in_("new_status_id", target_status_ids)
        .gte("changed_at", start_utc)
        .lte("changed_at", end_utc)
        .order("changed_at")
    )
    osc_rows = query.execute().data

    if not osc_rows:
        return build_snapshot([], start_date, end_date, now, range_type)

    # 2. Collect unique lead_ids and opportunity_ids
    lead_ids = set()
    opp_ids = set()
    for row in osc_rows:
        if row.get("lead_id"):
            lead_ids.add(row["lead_id"])
        if row.get("opportunity_id"):
            opp_ids.add(row["opportunity_id"])

    # 3. Fetch related data
    leads_data = _chunked_in_query(sb, "leads", "id", lead_ids)
    leads_map = {l["id"]: l for l in leads_data}

    # Fetch calls for these leads — include 30-min buffer before earliest transition
    earliest_change = min(row["changed_at"] for row in osc_rows)
    earliest_dt = datetime.fromisoformat(earliest_change.replace("Z", "+00:00"))
    calls_start = (earliest_dt - timedelta(minutes=30)).isoformat()
    calls_rows = _chunked_in_query(
        sb, "calls", "lead_id", lead_ids,
        select="lead_id,date_created,duration,status"
    )
    bulk_calls = {}
    for c in calls_rows:
        if c["date_created"] and c["date_created"] >= calls_start:
            lid = c["lead_id"]
            bulk_calls.setdefault(lid, []).append({
                "ts": c["date_created"],
                "dur": c.get("duration", 0),
                "st": c.get("status", ""),
            })

    # Fetch users
    users_rows = sb.table("users").select("id,name").execute().data
    users_map = {u["id"]: u["name"] for u in users_rows}

    # Fetch opportunities for AE assignment
    opps_data = _chunked_in_query(sb, "opportunities", "id", opp_ids, select="id,lead_id,user_id")
    lead_ae_map = {}
    for opp in opps_data:
        lid = opp.get("lead_id")
        uid = opp.get("user_id")
        if lid and uid and lid not in lead_ae_map:
            lead_ae_map[lid] = uid

    # 4. Classify each transition
    results = []
    seen_keys = set()
    for row in osc_rows:
        lead_id = row["lead_id"]
        changed_at = row["changed_at"]
        key = f"{lead_id}_{changed_at}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        lead = leads_map.get(lead_id, {})
        call_at = find_earliest_call(bulk_calls, lead_id, changed_at)
        pre_call_at = find_pre_trigger_call(bulk_calls, lead_id, changed_at)
        bucket, mins_to_call = classify(changed_at, call_at, pre_call_at, now)

        ae_user_id = lead_ae_map.get(lead_id) or row.get("user_id")
        ae_name = users_map.get(ae_user_id, "Unknown")

        new_status_id = row.get("new_status_id")
        transition = TARGET_STATUSES.get(new_status_id, "Active Scenario")

        results.append({
            "contact": lead.get("contact_name") or lead.get("display_name") or "(Unknown)",
            "ae": ae_name,
            "changedAt": changed_at,
            "callAt": call_at,
            "preCallAt": pre_call_at,
            "preCall": bool(pre_call_at),
            "minsToCall": mins_to_call,
            "bucket": bucket,
            "leadId": lead_id,
            "opportunityId": row.get("opportunity_id"),
            "transition": transition,
        })

    snapshot = build_snapshot(results, start_date, end_date, now, range_type)
    snapshot["_debug"] = {
        "start_utc": start_utc,
        "end_utc": end_utc,
        "osc_count": len(osc_rows),
        "results_count": len(results),
    }
    return snapshot
