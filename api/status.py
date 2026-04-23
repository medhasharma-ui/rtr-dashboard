"""
Vercel serverless function — returns current cron run status.

GET /api/status

Response:
  { "phase": "idle|init_leads|init_calls|processing|complete",
    "cursor": 50,
    "total": 120,
    "start_date": "2026-04-14",
    "end_date": "2026-04-21",
    "started_at": "...",
    "updated_at": "..." }
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            sb = create_client(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_SECRET_KEY"],
            )
            rows = sb.table("cron_state").select(
                "phase,cursor,total,start_date,end_date,started_at,updated_at"
            ).eq("id", "current").execute()

            if not rows.data:
                result = {"phase": "idle", "message": "No run state found. Hit /api/cron to start."}
            else:
                result = rows.data[0]

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
