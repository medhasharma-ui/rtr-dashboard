"""
Shared database helpers for normalized Supabase tables.

Provides upsert functions for each entity type and sync cursor management.
All upserts batch in chunks of 500 to stay under PostgREST size limits.
"""

import os
from datetime import datetime, timezone

from supabase import create_client

UPSERT_CHUNK = 500


def get_supabase():
    """Create and return a Supabase client using env vars."""
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )


def _chunked_upsert(sb, table, rows):
    """Upsert rows in chunks of UPSERT_CHUNK."""
    if not rows:
        return
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i : i + UPSERT_CHUNK]
        sb.table(table).upsert(chunk).execute()


def upsert_users(sb, users):
    """Upsert user records. `users` is a list of dicts with id, name, email."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"id": u["id"], "name": u["name"], "email": u.get("email"), "synced_at": now} for u in users]
    _chunked_upsert(sb, "users", rows)
    return len(rows)


def upsert_leads(sb, leads):
    """Upsert lead records. `leads` is a list of dicts with id, display_name, contact_name."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "id": l["id"],
            "display_name": l.get("display_name"),
            "contact_name": l.get("contact_name"),
            "synced_at": now,
        }
        for l in leads
    ]
    _chunked_upsert(sb, "leads", rows)
    return len(rows)


def upsert_opportunities(sb, opps):
    """Upsert opportunity records."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "id": o["id"],
            "lead_id": o.get("lead_id"),
            "status_id": o.get("status_id"),
            "status_label": o.get("status_label"),
            "pipeline_id": o.get("pipeline_id"),
            "user_id": o.get("user_id"),
            "created_at": o.get("created_at") or o.get("date_created"),
            "updated_at": o.get("updated_at") or o.get("date_updated"),
            "synced_at": now,
        }
        for o in opps
    ]
    _chunked_upsert(sb, "opportunities", rows)
    return len(rows)


def upsert_status_changes(sb, changes):
    """Upsert opportunity status change records."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "id": c["id"],
            "lead_id": c.get("lead_id"),
            "opportunity_id": c.get("opportunity_id"),
            "old_status_id": c.get("old_status_id"),
            "new_status_id": c.get("new_status_id"),
            "changed_at": c.get("date_created") or c.get("changed_at"),
            "user_id": c.get("user_id"),
            "synced_at": now,
        }
        for c in changes
    ]
    _chunked_upsert(sb, "opportunity_status_changes", rows)
    return len(rows)


def upsert_calls(sb, calls):
    """Upsert call activity records."""
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "id": c["id"],
            "lead_id": c.get("lead_id"),
            "user_id": c.get("user_id"),
            "date_created": c.get("date_created"),
            "duration": c.get("duration", 0),
            "status": c.get("status"),
            "synced_at": now,
        }
        for c in calls
    ]
    _chunked_upsert(sb, "calls", rows)
    return len(rows)


def get_sync_cursor(sb, entity_type="event_log"):
    """Get the last sync cursor for a given entity type. Returns dict or None."""
    rows = sb.table("sync_cursors").select("*").eq("entity_type", entity_type).execute()
    return rows.data[0] if rows.data else None


def set_sync_cursor(sb, entity_type="event_log", last_event_date=None):
    """Set/update the sync cursor for a given entity type."""
    now = datetime.now(timezone.utc).isoformat()
    sb.table("sync_cursors").upsert({
        "entity_type": entity_type,
        "last_event_date": last_event_date or now,
        "last_synced_at": now,
    }).execute()
