"""
Vercel serverless function — returns current cron run status.

GET /api/status              — returns status for both modes
GET /api/status?mode=mtd     — returns MTD run status only
GET /api/status?mode=recent  — returns recent run status only
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client

FIELDS = "phase,cursor,total,start_date,end_date,range_type,started_at,updated_at"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            sb = create_client(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_SECRET_KEY"],
            )
            params = parse_qs(urlparse(self.path).query)
            mode = params.get("mode", [None])[0]

            if mode:
                # Single mode
                rows = sb.table("cron_state").select(FIELDS).eq("id", mode).execute()
                if not rows.data:
                    result = {"mode": mode, "phase": "idle", "message": "No run state found."}
                else:
                    result = rows.data[0]
            else:
                # Both modes
                rows = sb.table("cron_state").select(FIELDS).in_("id", ["mtd", "recent"]).execute()
                result = {}
                for row in (rows.data or []):
                    result[row["id"]] = row
                if not result:
                    result = {"message": "No run state found. Hit /api/cron?mode=mtd to start."}

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
