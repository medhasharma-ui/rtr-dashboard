"""
Vercel serverless function — query normalized tables and compute dashboard JSON.

GET /api/dashboard
GET /api/dashboard?range=mtd
GET /api/dashboard?start=2026-04-01&end=2026-04-24

Returns the same JSON shape as the legacy /api/snapshot endpoint so the
frontend works unchanged.
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_query import query_dashboard

PT = ZoneInfo("America/Los_Angeles")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            now = datetime.now(timezone.utc)
            pt_now = now.astimezone(PT)

            range_param = params.get("range", ["mtd"])[0]
            start_param = params.get("start", [None])[0]
            end_param = params.get("end", [None])[0]

            if start_param and end_param:
                start_date = start_param
                end_date = end_param
                range_type = "custom"
            elif range_param == "mtd" or (not start_param and not end_param):
                start_date = pt_now.replace(day=1).strftime("%Y-%m-%d")
                end_date = pt_now.strftime("%Y-%m-%d")
                range_type = "mtd"
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
