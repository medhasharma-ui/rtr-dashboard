"""
Vercel serverless function — returns the latest dashboard snapshot.

GET /api/snapshot

Returns the most recent snapshot's data JSON from Supabase.
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
            rows = (
                sb.table("dashboard_snapshots")
                .select("data,generated_at")
                .order("generated_at", desc=True)
                .limit(1)
                .execute()
            )

            if not rows.data:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No snapshots found"}).encode())
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(rows.data[0]["data"]).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass
