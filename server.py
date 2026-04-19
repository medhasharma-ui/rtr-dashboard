#!/usr/bin/env python3
"""
RTR Dashboard Server
Serves the dashboard at http://rtr-dashboard.local
Pulls live data from Close CRM on startup and via /api/refresh endpoint.
"""

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

# Load .env from project root
PROJECT_DIR = Path(__file__).parent
load_dotenv(PROJECT_DIR / ".env")

# Import pull_data functions
sys.path.insert(0, str(PROJECT_DIR))
from pull_data import (
    fetch_rtr_transitions,
    fetch_lead_info,
    fetch_calls_for_lead,
    fetch_users,
    classify,
)

HOST = "rtr-dashboard.local"
PORT = 8080
DATA_FILE = PROJECT_DIR / "data" / "dashboard_data.json"


def get_api_key():
    key = os.environ.get("CLOSE_API_KEY")
    if not key:
        print("ERROR: No CLOSE_API_KEY found.")
        print("Create a .env file in the project root:")
        print(f"  echo 'CLOSE_API_KEY=your_key' > '{PROJECT_DIR}/.env'")
        sys.exit(1)
    return key


def pull_fresh_data(days=7):
    """Pull data from Close CRM and write to data/dashboard_data.json."""
    api_key = get_api_key()
    now = datetime.now(timezone.utc)
    end_date = now.strftime("%Y-%m-%d")
    start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"\n--- Pulling data from Close CRM ({start_date} to {end_date}) ---")

    transitions = fetch_rtr_transitions(api_key, start_date, end_date)
    if not transitions:
        print("No transitions found.")
        return {"error": "No transitions found", "start_date": start_date, "end_date": end_date}

    print("Fetching user directory...")
    users = fetch_users(api_key)

    results = []
    seen_keys = set()

    for i, t in enumerate(transitions):
        lead_id = t["lead_id"]
        changed_at = t["changed_at"]
        key = f"{lead_id}_{changed_at}"

        if key in seen_keys:
            continue
        seen_keys.add(key)

        print(f"  Processing {i+1}/{len(transitions)}: {lead_id}...")

        lead_info = fetch_lead_info(api_key, lead_id)
        call_at = fetch_calls_for_lead(api_key, lead_id, changed_at)
        bucket, mins_to_call = classify(changed_at, call_at, now)

        ae_user_id = lead_info.get("user_id") or t.get("user_id")
        ae_name = users.get(ae_user_id, "Unknown")

        results.append({
            "contact": lead_info["contact_name"],
            "ae": ae_name,
            "changedAt": changed_at,
            "callAt": call_at,
            "minsToCall": mins_to_call,
            "bucket": bucket,
            "leadId": lead_id,
            "opportunityId": t.get("opportunity_id"),
            "transition": t.get("transition", "Active Scenario"),
        })

    by_date = {}
    for r in results:
        date_key = r["changedAt"][:10]
        by_date.setdefault(date_key, []).append(r)

    output = {
        "generated_at": now.isoformat(),
        "start_date": start_date,
        "end_date": end_date,
        "total_leads": len(results),
        "by_date": by_date,
        "all": results,
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(output, f, indent=2)

    within = sum(1 for r in results if r["bucket"] == "within")
    after = sum(1 for r in results if r["bucket"] == "after")
    never = sum(1 for r in results if r["bucket"] == "never")
    pending = sum(1 for r in results if r["bucket"] == "pending")

    summary = {
        "status": "ok",
        "total": len(results),
        "within": within,
        "after": after,
        "never": never,
        "pending": pending,
        "start_date": start_date,
        "end_date": end_date,
    }
    print(f"\nDone! {len(results)} leads. Within:{within} After:{after} Never:{never} Pending:{pending}")
    return summary


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves static files from the project directory + API endpoints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/refresh":
            params = parse_qs(parsed.query)
            days = int(params.get("days", [7])[0])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # Run in background so the response returns immediately
            result = {"status": "refreshing", "days": days}
            self.wfile.write(json.dumps(result).encode())
            threading.Thread(target=pull_fresh_data, args=(days,), daemon=True).start()
            return

        if parsed.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            status = {"running": True}
            if DATA_FILE.exists():
                with open(DATA_FILE) as f:
                    d = json.load(f)
                status["generated_at"] = d.get("generated_at")
                status["total_leads"] = d.get("total_leads")
            self.wfile.write(json.dumps(status).encode())
            return

        super().do_GET()

    def log_message(self, format, *args):
        # Quieter logging — only log non-static requests
        if "/api/" in str(args[0]) if args else False:
            super().log_message(format, *args)


def main():
    # Verify API key exists before starting
    get_api_key()

    # Pull fresh data on startup
    print("Pulling fresh data on startup...")
    pull_fresh_data(days=7)

    # Start server
    server = HTTPServer((HOST, PORT), DashboardHandler)

    print(f"\n{'='*50}")
    print(f"  RTR Dashboard running at:")
    print(f"  http://{HOST}:{PORT}")
    print(f"{'='*50}")
    print(f"  Refresh data: http://{HOST}:{PORT}/api/refresh?days=7")
    print(f"  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
