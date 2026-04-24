"""
Vercel serverless function — returns dashboard data.

GET /api/snapshot             — relational query (default)
GET /api/snapshot?source=snapshot — legacy JSONB snapshot

The relational path queries normalized tables and computes buckets on the fly.
The snapshot path returns the latest pre-computed JSONB blob (original behavior).

Supports ?range=mtd, ?start=...&end=... params for the relational path.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client
from dashboard_query import query_dashboard

PT_FIXED = timezone(timedelta(hours=-7))  # approximate Pacific for legacy path
PT = ZoneInfo("America/Los_Angeles")


def _pt_date_key(dt):
    return dt.astimezone(PT_FIXED).strftime("%Y-%m-%d")


def _get_snapshot_data():
    """Legacy path: fetch latest JSONB snapshot, merge with recent overlay."""
    sb = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
    )
    rows = (
        sb.table("dashboard_snapshots")
        .select("data,generated_at")
        .order("generated_at", desc=True)
        .limit(10)
        .execute()
    )

    if not rows.data:
        return {"error": "No snapshots found"}

    full_row = None
    recent_row = None
    for row in rows.data:
        rtype = row["data"].get("range_type", "mtd")
        if rtype in ("mtd", "custom") and not full_row:
            full_row = row
        elif rtype == "recent" and not recent_row:
            recent_row = row
        if full_row and recent_row:
            break

    if not full_row:
        return {"error": "No MTD snapshot found"}

    full = full_row["data"]
    recent = recent_row["data"] if recent_row else None
    use_recent = recent and recent["generated_at"] > full["generated_at"]

    if use_recent:
        now = datetime.now(timezone.utc)
        today = _pt_date_key(now)
        yesterday = _pt_date_key(now - timedelta(days=1))
        merged = dict(full.get("by_date", {}))
        merged.pop(today, None)
        merged.pop(yesterday, None)
        if today in recent.get("by_date", {}):
            merged[today] = recent["by_date"][today]
        if yesterday in recent.get("by_date", {}):
            merged[yesterday] = recent["by_date"][yesterday]
        merged_all = [item for leads in merged.values() for item in leads]
        result = {**full, "by_date": merged, "all": merged_all,
                  "_recent_generated_at": recent["generated_at"]}
    else:
        result = full

    return result


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            source = params.get("source", ["relational"])[0]

            if source == "snapshot":
                result = _get_snapshot_data()
            else:
                now = datetime.now(timezone.utc)
                pt_now = now.astimezone(PT)

                start_param = params.get("start", [None])[0]
                end_param = params.get("end", [None])[0]

                if start_param and end_param:
                    start_date = start_param
                    end_date = end_param
                    range_type = "custom"
                else:
                    start_date = pt_now.replace(day=1).strftime("%Y-%m-%d")
                    end_date = pt_now.strftime("%Y-%m-%d")
                    range_type = "mtd"

                result = query_dashboard(start_date, end_date, range_type)

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
