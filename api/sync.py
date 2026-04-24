"""
Vercel serverless function — trigger incremental sync.

GET /api/sync

Fetches new data from Close CRM since last sync and upserts into
normalized Supabase tables. Designed to be called by an external cron
(e.g., cron-job.org) every 15 minutes.

Requires env vars: CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync_events import run_sync


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            api_key = os.environ.get("CLOSE_API_KEY")
            if not api_key:
                self._json(500, {"error": "Missing CLOSE_API_KEY env var"})
                return

            result = run_sync(api_key=api_key)
            status = 500 if result.get("error") else 200
            self._json(status, result)

        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass
