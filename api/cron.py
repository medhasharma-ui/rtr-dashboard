"""
Vercel serverless function — batch-processes Close CRM data.

Each call processes one small step, picking up where the last call left off.
An external caller loops until /api/status reports phase="complete".

Phase state machine:
  idle → init_leads → init_calls → processing → complete

Endpoints:
  GET /api/cron          — process next step (or start new run if idle)
  GET /api/cron?reset=1  — force-reset state and start a fresh run
  GET /api/cron?dry=1    — dry-run mode: skips snapshot insert, returns data in response

Requires env vars: CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY

Supabase table required — see setup.sql
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

# Project root → importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client

from pull_data import (
    close_get,
    fetch_calls_chunk,
    fetch_lead_ids,
    fetch_lead_infos_parallel,
    fetch_status_changes_for_lead,
    fetch_users,
    process_transitions,
    build_snapshot,
    _calls_rows_to_dict,
    PT,
    MAX_WORKERS,
)

BATCH_SIZE = int(os.environ.get("CRON_BATCH_SIZE", "30"))
CALLS_PAGES_PER_STEP = int(os.environ.get("CALLS_PAGES_PER_STEP", "20"))


# ── Supabase helpers ──

def get_supabase():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )


def get_state(sb):
    rows = sb.table("cron_state").select("*").eq("id", "current").execute()
    return rows.data[0] if rows.data else None


def save_state(sb, data):
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("cron_state").upsert({**data, "id": "current"}).execute()


# ── Phases ──

def do_init_leads(sb, api_key, dry=False):
    """Step 1: Fetch lead_ids + users in parallel. ~3-4s."""
    t_start = time.time()
    now = datetime.now(timezone.utc)
    pt_now = now.astimezone(PT)
    end_date = pt_now.strftime("%Y-%m-%d")
    start_date = (pt_now - timedelta(days=7)).strftime("%Y-%m-%d")
    api_end = now.isoformat()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_leads = executor.submit(fetch_lead_ids, api_key, start_date, api_end)
        f_users = executor.submit(fetch_users, api_key)
        lead_ids = f_leads.result()
        users = f_users.result()

    print(f"[init_leads] {len(lead_ids)} leads, {len(users)} users ({time.time()-t_start:.1f}s)")

    if not lead_ids:
        save_state(sb, {
            "phase": "complete", "cursor": 0, "total": 0,
            "lead_ids": [], "bulk_calls": {}, "users": {},
            "results": [],
            "start_date": start_date, "end_date": end_date,
            "api_end": api_end, "started_at": now.isoformat(), "dry": dry,
        })
        return {"status": "complete", "total_leads": 0, "message": "No leads found for this period"}

    t0 = time.time()
    # Store call-fetch progress inside bulk_calls (it's empty {} during init_calls)
    save_state(sb, {
        "phase": "init_calls",
        "cursor": 0,
        "total": len(lead_ids),
        "lead_ids": lead_ids,
        "bulk_calls": {"_rows": [], "_skip": 0},
        "users": users,
        "results": [],
        "start_date": start_date,
        "end_date": end_date,
        "api_end": api_end,
        "started_at": now.isoformat(),
        "dry": dry,
    })
    print(f"[init_leads] save_state: {time.time()-t0:.1f}s")

    elapsed = round(time.time() - t_start, 1)
    print(f"[init_leads] TOTAL: {elapsed}s")
    return {
        "status": "init_calls",
        "total_leads": len(lead_ids),
        "elapsed_s": elapsed,
        "message": f"Leads fetched — now fetching calls...",
    }


def do_init_calls(sb, api_key, state):
    """Step 2 (repeated): Fetch calls in chunks of CALLS_PAGES_PER_STEP pages. ~4-5s per step."""
    t_start = time.time()
    bc = state.get("bulk_calls", {})
    skip_from = bc.get("_skip", 0)
    call_rows = bc.get("_rows", [])

    rows, done = fetch_calls_chunk(
        api_key, state["start_date"], state["api_end"],
        skip_from=skip_from, max_pages=CALLS_PAGES_PER_STEP,
    )
    call_rows.extend(rows)

    if done:
        # All calls fetched — convert to dict and move to processing phase
        bulk_calls = _calls_rows_to_dict(call_rows)
        total_calls = sum(len(v) for v in bulk_calls.values())

        t0 = time.time()
        save_state(sb, {
            "phase": "processing",
            "cursor": 0,
            "total": state["total"],
            "lead_ids": state["lead_ids"],
            "bulk_calls": bulk_calls,
            "users": state["users"],
            "results": [],
            "start_date": state["start_date"],
            "end_date": state["end_date"],
            "api_end": state["api_end"],
            "started_at": state["started_at"],
            "dry": state.get("dry", False),
        })
        print(f"[init_calls] save_state: {time.time()-t0:.1f}s")

        elapsed = round(time.time() - t_start, 1)
        print(f"[init_calls] DONE: {len(call_rows)} rows, {total_calls} calls ({elapsed}s)")
        return {
            "status": "processing",
            "total_leads": state["total"],
            "calls_cached": total_calls,
            "elapsed_s": elapsed,
            "message": f"Calls fetched — processing {state['total']} leads in batches of {BATCH_SIZE}",
        }
    else:
        # More calls to fetch — save partial progress inside bulk_calls
        new_skip = skip_from + CALLS_PAGES_PER_STEP * 100

        t0 = time.time()
        save_state(sb, {
            **state,
            "bulk_calls": {"_rows": call_rows, "_skip": new_skip},
        })
        print(f"[init_calls] save_state: {time.time()-t0:.1f}s")

        elapsed = round(time.time() - t_start, 1)
        print(f"[init_calls] partial: {len(call_rows)} rows so far, next skip={new_skip} ({elapsed}s)")
        return {
            "status": "init_calls",
            "calls_fetched_so_far": len(call_rows),
            "elapsed_s": elapsed,
            "message": f"Fetching calls (page {new_skip // 100})...",
        }


def do_batch(sb, api_key, state):
    """Process next batch of leads."""
    t_start = time.time()
    now = datetime.now(timezone.utc)
    cursor = state["cursor"]
    lead_ids = state["lead_ids"]
    bulk_calls = state["bulk_calls"]
    users = state["users"]
    results = state["results"]
    start_date = state["start_date"]
    api_end = state["api_end"]

    # Track retries — if Vercel kills us mid-batch, cursor stays the same.
    # After 3 attempts on the same cursor, skip ahead to avoid infinite loop.
    retries = state.get("retries", 0)
    if retries >= 3:
        print(f"[batch] Skipping leads {cursor}–{cursor+BATCH_SIZE} after {retries} failed attempts")
        new_cursor = min(cursor + BATCH_SIZE, len(lead_ids))
        if new_cursor >= len(lead_ids):
            return do_finalize(sb, state, now, dry=state.get("dry", False))
        save_state(sb, {**state, "cursor": new_cursor, "retries": 0})
        return {
            "status": "processing",
            "processed_leads": new_cursor,
            "total_leads": len(lead_ids),
            "skipped": True,
            "message": f"Skipped batch at cursor {cursor} after {retries} timeouts",
        }
    # Bump retry counter now — if we get killed, next hit sees retries+1.
    # On success, save_state at end resets it to 0.
    sb.table("cron_state").update({
        "retries": retries + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", "current").execute()

    batch = lead_ids[cursor:cursor + BATCH_SIZE]
    if not batch:
        return do_finalize(sb, state, now, dry=state.get("dry", False))

    print(f"[batch] Processing leads {cursor}–{cursor+len(batch)} of {len(lead_ids)} (attempt {retries+1})")

    # Parallel: fetch status changes for this batch (cap workers to limit rate-limit spikes)
    t0 = time.time()
    transitions = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(
                fetch_status_changes_for_lead, api_key, lid, start_date, api_end
            ): lid
            for lid in batch
        }
        for future in as_completed(futures):
            try:
                transitions.extend(future.result())
            except Exception:
                pass
    print(f"[batch] status_changes: {time.time()-t0:.1f}s → {len(transitions)} transitions")

    # Parallel: fetch lead info only for leads that actually had transitions
    t0 = time.time()
    t_lead_ids = list(set(t["lead_id"] for t in transitions))
    lead_infos = fetch_lead_infos_parallel(api_key, t_lead_ids) if t_lead_ids else {}
    print(f"[batch] lead_infos: {time.time()-t0:.1f}s → {len(lead_infos)} leads")

    # Classify using pre-fetched bulk calls
    t0 = time.time()
    batch_results = process_transitions(transitions, bulk_calls, lead_infos, users, now)
    results.extend(batch_results)
    print(f"[batch] classify: {time.time()-t0:.1f}s → {len(batch_results)} results")

    new_cursor = cursor + len(batch)

    if new_cursor >= len(lead_ids):
        return do_finalize(sb, {**state, "results": results}, now, dry=state.get("dry", False))

    t0 = time.time()
    save_state(sb, {
        "phase": "processing",
        "cursor": new_cursor,
        "total": len(lead_ids),
        "lead_ids": lead_ids,
        "bulk_calls": bulk_calls,
        "users": users,
        "results": results,
        "start_date": state["start_date"],
        "end_date": state["end_date"],
        "api_end": api_end,
        "started_at": state["started_at"],
        "retries": 0,
    })
    print(f"[batch] save_state: {time.time()-t0:.1f}s")

    elapsed = round(time.time() - t_start, 1)
    print(f"[batch] TOTAL: {elapsed}s")

    return {
        "status": "processing",
        "processed_leads": new_cursor,
        "total_leads": len(lead_ids),
        "batch_transitions": len(transitions),
        "batch_results": len(batch_results),
        "results_so_far": len(results),
        "elapsed_s": elapsed,
    }


def do_finalize(sb, state, now, dry=False):
    """Write snapshot to dashboard_snapshots and mark run complete."""
    t_start = time.time()
    results = state["results"]
    snapshot = build_snapshot(results, state["start_date"], state["end_date"], now)

    if not dry:
        t0 = time.time()
        sb.table("dashboard_snapshots").insert({
            "generated_at": snapshot["generated_at"],
            "data": snapshot,
        }).execute()
        print(f"[finalize] insert_snapshot: {time.time()-t0:.1f}s")
    else:
        print("[finalize] dry run — skipping snapshot insert")

    # Clear large fields from state to keep the row small
    t0 = time.time()
    save_state(sb, {
        "phase": "complete",
        "cursor": state["total"],
        "total": state["total"],
        "lead_ids": [],
        "bulk_calls": {},
        "users": {},
        "results": [],
        "start_date": state["start_date"],
        "end_date": state["end_date"],
        "api_end": state.get("api_end"),
        "started_at": state["started_at"],
    })

    within = sum(1 for r in results if r["bucket"] == "within")
    after = sum(1 for r in results if r["bucket"] == "after")
    never = sum(1 for r in results if r["bucket"] == "never")
    pending = sum(1 for r in results if r["bucket"] == "pending")

    print(f"[finalize] save_state: {time.time()-t0:.1f}s")
    print(f"[finalize] TOTAL: {time.time()-t_start:.1f}s")

    result = {
        "status": "complete",
        "total_leads": len(results),
        "within": within,
        "after": after,
        "never": never,
        "pending": pending,
        "elapsed_s": round(time.time() - t_start, 1),
    }
    if dry:
        result["dry_run"] = True
        result["snapshot"] = snapshot
    return result


# ── Handler ──

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            sb = get_supabase()
            api_key = os.environ.get("CLOSE_API_KEY")
            if not api_key:
                raise RuntimeError("Missing CLOSE_API_KEY env var")

            reset = params.get("reset", [""])[0] == "1"
            dry = params.get("dry", [""])[0] == "1"

            if reset:
                result = do_init_leads(sb, api_key, dry=dry)
            else:
                state = get_state(sb)
                if not state or state["phase"] == "idle":
                    result = do_init_leads(sb, api_key, dry=dry)
                elif state["phase"] == "init_calls":
                    result = do_init_calls(sb, api_key, state)
                elif state["phase"] == "processing":
                    result = do_batch(sb, api_key, state)
                elif state["phase"] == "complete":
                    result = {
                        "status": "complete",
                        "message": "Run already complete. Hit /api/cron?reset=1 to start a new run.",
                        "started_at": state.get("started_at"),
                    }
                else:
                    result = {"status": "error", "message": f"Unknown phase: {state.get('phase')}"}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass
