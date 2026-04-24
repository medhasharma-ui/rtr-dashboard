"""
Vercel serverless function — incremental sync with state machine.

Each GET processes one step (~5s max), picking up where the last left off.
An external caller loops until status="complete".

Phase state machine:
  idle → fetch_opps → fetch_changes → fetch_calls → fetch_leads → complete

Endpoints:
  GET /api/sync          — process next sync step
  GET /api/sync?reset=1  — force-reset and start a fresh run

Requires env vars: CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY

State persisted in cron_state table (id='sync').
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync_events import (
    fetch_recent_opportunities,
    fetch_status_changes_for_leads,
    fetch_calls_paginated,
)
from pull_data import fetch_lead_infos_parallel
from db import (
    get_supabase,
    upsert_leads,
    upsert_opportunities,
    upsert_status_changes,
    upsert_calls,
    get_sync_cursor,
    set_sync_cursor,
)

# Tuning knobs — override via env vars
CHANGES_BATCH = int(os.environ.get("SYNC_CHANGES_BATCH", "30"))
CALLS_PAGES_PER_STEP = int(os.environ.get("SYNC_CALLS_PAGES", "10"))
LEADS_BATCH = int(os.environ.get("SYNC_LEADS_BATCH", "30"))

STATE_ID = "sync"


# ── State helpers ──

def get_state(sb):
    rows = sb.table("cron_state").select("*").eq("id", STATE_ID).execute()
    return rows.data[0] if rows.data else None


def save_state(sb, data):
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("cron_state").upsert({**data, "id": STATE_ID}).execute()


# ── Phases ──

def do_fetch_opps(sb, api_key):
    """Phase 1: Read sync cursor, fetch recent opportunities, upsert, collect lead_ids."""
    t0 = time.time()
    now = datetime.now(timezone.utc)

    cursor = get_sync_cursor(sb, entity_type="event_log")
    if not cursor:
        return {"status": "error", "error": "No sync cursor found. Run initial_load.py first."}

    last_sync = cursor["last_event_date"]
    print(f"[sync:fetch_opps] last_sync={last_sync}")

    raw_opps = fetch_recent_opportunities(api_key, last_sync)
    lead_ids = list(set(o.get("lead_id") for o in raw_opps if o.get("lead_id")))

    if raw_opps:
        opp_rows = [{
            "id": o["id"], "lead_id": o.get("lead_id"),
            "status_id": o.get("status_id"), "status_label": o.get("status_label"),
            "pipeline_id": o.get("pipeline_id"), "user_id": o.get("user_id"),
            "created_at": o.get("date_created"), "updated_at": o.get("date_updated"),
        } for o in raw_opps]
        upsert_opportunities(sb, opp_rows)

    save_state(sb, {
        "phase": "fetch_changes", "cursor": 0, "total": len(lead_ids),
        "lead_ids": lead_ids,
        "bulk_calls": {
            "last_sync": last_sync, "now": now.isoformat(),
            "call_lead_ids": [],
            "stats": {"opportunities": len(raw_opps), "status_changes": 0,
                       "calls": 0, "new_leads": 0},
        },
        "users": {}, "results": [],
        "started_at": now.isoformat(),
    })

    elapsed = round(time.time() - t0, 1)
    print(f"[sync:fetch_opps] {len(raw_opps)} opps, {len(lead_ids)} leads ({elapsed}s)")
    return {
        "status": "fetch_changes",
        "opportunities": len(raw_opps), "leads_to_process": len(lead_ids),
        "elapsed_s": elapsed,
        "message": f"Fetched {len(raw_opps)} opps — processing changes for {len(lead_ids)} leads",
    }


def do_fetch_changes(sb, api_key, state):
    """Phase 2 (repeated): Fetch status changes for a batch of leads, upsert."""
    t0 = time.time()
    pos = state["cursor"]
    lead_ids = state["lead_ids"]
    meta = state.get("bulk_calls", {})
    last_sync = meta["last_sync"]
    stats = meta.get("stats", {})

    batch = lead_ids[pos:pos + CHANGES_BATCH]
    if not batch:
        save_state(sb, {**state, "phase": "fetch_calls", "cursor": 0, "total": 0})
        return {"status": "fetch_calls", "status_changes": 0,
                "message": "No leads — fetching calls..."}

    print(f"[sync:fetch_changes] leads {pos}–{pos+len(batch)} of {len(lead_ids)}")
    changes = fetch_status_changes_for_leads(api_key, batch, last_sync)
    if changes:
        upsert_status_changes(sb, changes)

    new_pos = pos + len(batch)
    stats["status_changes"] = stats.get("status_changes", 0) + len(changes)
    done = new_pos >= len(lead_ids)
    next_phase = "fetch_calls" if done else "fetch_changes"

    save_state(sb, {
        **state,
        "phase": next_phase,
        "cursor": 0 if done else new_pos,
        "total": 0 if done else state["total"],
        "bulk_calls": {**meta, "stats": stats},
    })

    elapsed = round(time.time() - t0, 1)
    print(f"[sync:fetch_changes] {len(changes)} changes ({elapsed}s)")
    return {
        "status": next_phase,
        "processed_leads": min(new_pos, len(lead_ids)), "total_leads": len(lead_ids),
        "batch_changes": len(changes), "total_changes": stats["status_changes"],
        "elapsed_s": elapsed,
        "message": f"Changes: {min(new_pos, len(lead_ids))}/{len(lead_ids)} leads"
                   + (" — fetching calls..." if done else ""),
    }


def do_fetch_calls(sb, api_key, state):
    """Phase 3 (repeated): Fetch calls in paginated chunks, upsert."""
    t0 = time.time()
    meta = state.get("bulk_calls", {})
    last_sync = meta["last_sync"]
    stats = meta.get("stats", {})
    skip_from = state["cursor"]
    call_lead_ids = meta.get("call_lead_ids", [])

    call_rows, done, next_skip = fetch_calls_paginated(
        api_key, last_sync, skip_from=skip_from, max_pages=CALLS_PAGES_PER_STEP,
    )

    if call_rows:
        upsert_calls(sb, call_rows)
        call_lead_ids.extend(c["lead_id"] for c in call_rows if c.get("lead_id"))

    stats["calls"] = stats.get("calls", 0) + len(call_rows)

    if done:
        # Merge opp lead_ids + call lead_ids for new-lead check
        all_lead_ids = list(set(state.get("lead_ids", [])) | set(call_lead_ids))
        save_state(sb, {
            **state,
            "phase": "fetch_leads", "cursor": 0,
            "total": len(all_lead_ids), "lead_ids": all_lead_ids,
            "bulk_calls": {**meta, "stats": stats, "call_lead_ids": []},
        })
        elapsed = round(time.time() - t0, 1)
        print(f"[sync:fetch_calls] done — {stats['calls']} total calls ({elapsed}s)")
        return {
            "status": "fetch_leads",
            "calls": stats["calls"], "leads_to_check": len(all_lead_ids),
            "elapsed_s": elapsed,
            "message": f"Fetched {stats['calls']} calls — checking for new leads...",
        }
    else:
        save_state(sb, {
            **state,
            "cursor": next_skip,
            "bulk_calls": {**meta, "stats": stats, "call_lead_ids": call_lead_ids},
        })
        elapsed = round(time.time() - t0, 1)
        print(f"[sync:fetch_calls] partial — {stats['calls']} calls so far ({elapsed}s)")
        return {
            "status": "fetch_calls",
            "calls_so_far": stats["calls"], "elapsed_s": elapsed,
            "message": f"Fetching calls (page {next_skip // 100})...",
        }


def do_fetch_leads(sb, api_key, state):
    """Phase 4 (repeated): Find missing leads in DB, fetch info, upsert."""
    t0 = time.time()
    meta = state.get("bulk_calls", {})
    stats = meta.get("stats", {})
    all_lead_ids = state.get("lead_ids", [])

    # Find which leads are missing from the DB
    missing = []
    if all_lead_ids:
        existing_ids = set()
        for i in range(0, len(all_lead_ids), 100):
            chunk = all_lead_ids[i:i + 100]
            rows = sb.table("leads").select("id").in_("id", chunk).execute()
            existing_ids.update(r["id"] for r in rows.data)
        missing = [lid for lid in all_lead_ids if lid not in existing_ids]

    if not missing:
        print(f"[sync:fetch_leads] no new leads")
        return do_complete(sb, state, stats)

    # Fetch a batch of missing leads
    batch = missing[:LEADS_BATCH]
    print(f"[sync:fetch_leads] {len(missing)} missing, fetching batch of {len(batch)}")

    lead_infos = fetch_lead_infos_parallel(api_key, batch)
    lead_rows = [{
        "id": lid,
        "display_name": info.get("lead_name"),
        "contact_name": info.get("contact_name"),
    } for lid, info in lead_infos.items()]
    upsert_leads(sb, lead_rows)
    stats["new_leads"] = stats.get("new_leads", 0) + len(lead_rows)

    if len(missing) > LEADS_BATCH:
        # More missing leads — stay in this phase (next call re-queries DB, finds fewer)
        save_state(sb, {**state, "bulk_calls": {**meta, "stats": stats}})
        elapsed = round(time.time() - t0, 1)
        print(f"[sync:fetch_leads] {len(lead_rows)} upserted, {len(missing) - LEADS_BATCH} remaining ({elapsed}s)")
        return {
            "status": "fetch_leads",
            "new_leads_so_far": stats["new_leads"],
            "remaining": len(missing) - LEADS_BATCH,
            "elapsed_s": elapsed,
            "message": f"Fetched {stats['new_leads']} new leads, ~{len(missing) - LEADS_BATCH} remaining...",
        }

    return do_complete(sb, state, stats)


def do_complete(sb, state, stats):
    """Phase 5: Update sync cursor, clear state."""
    meta = state.get("bulk_calls", {})
    now_iso = meta["now"]

    set_sync_cursor(sb, entity_type="event_log", last_event_date=now_iso)

    save_state(sb, {
        "phase": "complete", "cursor": 0, "total": 0,
        "lead_ids": [], "bulk_calls": {"stats": stats},
        "users": {}, "results": [],
        "started_at": state.get("started_at"),
    })

    print(f"[sync:complete] opps={stats.get('opportunities', 0)} changes={stats.get('status_changes', 0)} "
          f"calls={stats.get('calls', 0)} new_leads={stats.get('new_leads', 0)}")
    return {
        "status": "complete",
        **stats,
        "message": f"Sync complete: {stats.get('opportunities', 0)} opps, "
                   f"{stats.get('status_changes', 0)} changes, "
                   f"{stats.get('calls', 0)} calls, "
                   f"{stats.get('new_leads', 0)} new leads",
    }


# ── Handler ──

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            sb = get_supabase()
            api_key = os.environ.get("CLOSE_API_KEY")
            if not api_key:
                self._json(500, {"error": "Missing CLOSE_API_KEY env var"})
                return

            reset = params.get("reset", [""])[0] == "1"

            if reset:
                result = do_fetch_opps(sb, api_key)
            else:
                state = get_state(sb)
                phase = state["phase"] if state else None

                if phase in (None, "idle"):
                    result = do_fetch_opps(sb, api_key)
                elif phase == "complete":
                    result = {
                        "status": "complete",
                        "message": "Sync already complete. Use /api/sync?reset=1 to start a new run.",
                        "started_at": state.get("started_at"),
                        **(state.get("bulk_calls", {}).get("stats", {})),
                    }
                elif phase == "fetch_changes":
                    result = do_fetch_changes(sb, api_key, state)
                elif phase == "fetch_calls":
                    result = do_fetch_calls(sb, api_key, state)
                elif phase == "fetch_leads":
                    result = do_fetch_leads(sb, api_key, state)
                else:
                    result = {"status": "error", "message": f"Unknown phase: {phase}"}

            self._json(200, result)

        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass
