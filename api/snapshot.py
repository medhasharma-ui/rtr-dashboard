"""
Vercel serverless function — returns the latest dashboard snapshot.

GET /api/snapshot

Fetches up to 10 recent snapshots, finds the latest MTD/custom snapshot
as the base, and overlays any fresher "recent" (today+yesterday) snapshot
on top.  Returns a single merged JSON to the frontend.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client

PT = timezone(timedelta(hours=-7))  # approximate Pacific


def _pt_date_key(dt):
    return dt.astimezone(PT).strftime("%Y-%m-%d")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
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
                self._json(200, {"error": "No snapshots found"})
                return

            # Find the latest MTD/custom snapshot and the latest "recent" snapshot
            full_row = None
            recent_row = None
            for row in rows.data:
                rtype = row["data"].get("range_type", "mtd")  # legacy untagged = mtd
                if rtype in ("mtd", "custom") and not full_row:
                    full_row = row
                elif rtype == "recent" and not recent_row:
                    recent_row = row
                if full_row and recent_row:
                    break

            if not full_row:
                self._json(200, {"error": "No MTD snapshot found"})
                return

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
