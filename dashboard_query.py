"""
Shared dashboard query logic for relational tables.

Used by both api/dashboard.py and api/snapshot.py to compute dashboard JSON
from normalized Supabase tables via direct Postgres connection (psycopg2).
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

from pull_data import build_snapshot

PT = ZoneInfo("America/Los_Angeles")

RTR_STATUS_ID = "stat_AZ0tc4F8UzLQJyVG9vLH23R5RpMnIZdUkiNH7xvvVeb"
TARGET_STATUS_IDS = [
    "stat_Pn5zo8keGKa8rK4QCbg1sQAt72vREwPDPcZ9MyXv9Wf",  # Active Scenario
    "stat_ES08dw9Ij4gVsMrcuCtmviVwlJ0COaYJIrUgJtBWEtk",  # Declined Scenario
    "stat_RIXpsfGd3QDdTzdYQU16XLVaj1M6h4X6JV8qsZ8d7tW",  # Addl Info Needed
]

DASHBOARD_SQL = """
WITH osc AS (
    SELECT DISTINCT ON (lead_id, changed_at)
        id, lead_id, opportunity_id, new_status_id, changed_at, user_id
    FROM opportunity_status_changes
    WHERE old_status_id = %(rtr_status)s
      AND new_status_id = ANY(%(target_statuses)s)
      AND changed_at >= %(start_utc)s
      AND changed_at <= %(end_utc)s
    ORDER BY lead_id, changed_at
)
SELECT
    COALESCE(l.contact_name, l.display_name, '(Unknown)') AS contact,
    COALESCE(u.name, 'Unknown') AS ae,
    osc.changed_at AS "changedAt",
    post_call.date_created AS "callAt",
    pre_call.date_created AS "preCallAt",
    (pre_call.date_created IS NOT NULL) AS "preCall",
    CASE
        WHEN post_call.date_created IS NOT NULL THEN
            ROUND(EXTRACT(EPOCH FROM (post_call.date_created - osc.changed_at)) / 60.0, 1)
        ELSE NULL
    END AS "minsToCall",
    CASE
        WHEN post_call.date_created IS NOT NULL AND (
            pre_call.date_created IS NOT NULL
            OR EXTRACT(EPOCH FROM (post_call.date_created - osc.changed_at)) / 60.0 <= 120
        ) THEN 'within'
        WHEN post_call.date_created IS NOT NULL THEN 'after'
        WHEN pre_call.date_created IS NOT NULL THEN 'within'
        WHEN EXTRACT(EPOCH FROM (%(now)s - osc.changed_at)) / 60.0 < 120 THEN 'pending'
        ELSE 'never'
    END AS bucket,
    osc.lead_id AS "leadId",
    osc.opportunity_id AS "opportunityId",
    CASE osc.new_status_id
        WHEN 'stat_Pn5zo8keGKa8rK4QCbg1sQAt72vREwPDPcZ9MyXv9Wf' THEN 'Active Scenario'
        WHEN 'stat_ES08dw9Ij4gVsMrcuCtmviVwlJ0COaYJIrUgJtBWEtk' THEN 'Declined Scenario'
        WHEN 'stat_RIXpsfGd3QDdTzdYQU16XLVaj1M6h4X6JV8qsZ8d7tW' THEN 'Addl Info Needed'
        ELSE 'Active Scenario'
    END AS transition
FROM osc
LEFT JOIN leads l ON l.id = osc.lead_id
LEFT JOIN LATERAL (
    SELECT o.user_id
    FROM opportunities o
    WHERE o.id = osc.opportunity_id
    LIMIT 1
) opp ON true
LEFT JOIN users u ON u.id = COALESCE(opp.user_id, osc.user_id)
LEFT JOIN LATERAL (
    SELECT c.date_created
    FROM calls c
    WHERE c.lead_id = osc.lead_id
      AND c.date_created >= osc.changed_at
    ORDER BY c.date_created
    LIMIT 1
) post_call ON true
LEFT JOIN LATERAL (
    SELECT c.date_created
    FROM calls c
    WHERE c.lead_id = osc.lead_id
      AND c.date_created < osc.changed_at
      AND c.date_created >= osc.changed_at - INTERVAL '30 minutes'
      AND c.duration > 0
      AND c.status = 'completed'
    ORDER BY c.date_created
    LIMIT 1
) pre_call ON true
ORDER BY osc.changed_at
"""


def query_dashboard(start_date, end_date, range_type="custom"):
    """Query relational tables via direct Postgres and compute dashboard buckets.

    start_date/end_date are PT date strings (YYYY-MM-DD). We convert to UTC
    bounds so that records landing on e.g. April 23 PT (which may be April 24
    UTC) are included correctly.

    Returns the same JSON shape as build_snapshot().
    """
    now = datetime.now(timezone.utc)

    # Convert PT date range → UTC bounds
    start_pt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=PT)
    start_utc = start_pt.astimezone(timezone.utc)
    end_pt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=PT
    )
    end_utc = end_pt.astimezone(timezone.utc)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(DASHBOARD_SQL, {
                "rtr_status": RTR_STATUS_ID,
                "target_statuses": TARGET_STATUS_IDS,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "now": now,
            })
            rows = cur.fetchall()
    finally:
        conn.close()

    # Convert datetime/Decimal objects to JSON-serializable types
    results = []
    for row in rows:
        r = dict(row)
        r["changedAt"] = r["changedAt"].isoformat() if r["changedAt"] else None
        r["callAt"] = r["callAt"].isoformat() if r["callAt"] else None
        r["preCallAt"] = r["preCallAt"].isoformat() if r["preCallAt"] else None
        r["minsToCall"] = float(r["minsToCall"]) if r["minsToCall"] is not None else None
        results.append(r)

    return build_snapshot(results, start_date, end_date, now, range_type)
