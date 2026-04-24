# Speed-to-Call Dashboard 2.0

## What this is
A sales ops dashboard tracking whether AEs called a lead within 2 hours of an opportunity moving from **Ready to Respond** → **Active Scenario** in Close CRM.

## Close CRM Data Model
- **Lead**: The contact/company record — who gets called
- **Opportunity**: Lives inside a lead — this is what changes status
- **Opportunity status**: "Ready to Respond" → "Active Scenario" is the trigger
- **Call activity**: Logged against the lead — what we check post-trigger

## Key IDs (Close CRM - REQ Pipeline)
- Pipeline: `pipe_5VzsEaw8Df23USMhIwmMfz` (REQ)
- Ready to respond: `stat_AZ0tc4F8UzLQJyVG9vLH23R5RpMnIZdUkiNH7xvvVeb`
- Active Scenario: `stat_Pn5zo8keGKa8rK4QCbg1sQAt72vREwPDPcZ9MyXv9Wf`
- Declined Scenario: `stat_ES08dw9Ij4gVsMrcuCtmviVwlJ0COaYJIrUgJtBWEtk`
- Additional information needed: `stat_RIXpsfGd3QDdTzdYQU16XLVaj1M6h4X6JV8qsZ8d7tW`

## Tracked Transitions (all from Ready to Respond)
1. RTR → Active Scenario
2. RTR → Declined Scenario
3. RTR → Additional information needed

## Classification Buckets
| Bucket | Condition |
|--------|-----------|
| Called within 2 hrs | Earliest post-trigger call ≤ 120 mins after status change |
| Called after 2 hrs | Earliest post-trigger call > 120 mins after status change |
| Never called | No call activity found after status change |
| Pending | Status changed < 2 hrs ago — still within SLA window |

## Architecture

### Data Storage (two layers)

**Normalized relational tables** (primary, v2):
- 6 tables in Supabase: `users`, `leads`, `opportunities`, `opportunity_status_changes`, `calls`, `sync_cursors`
- One-time full load via `initial_load.py`, then incremental sync every 15 min via `sync_events.py` (Close Event Log API)
- Dashboard JSON computed on-the-fly by querying relational tables (`/api/snapshot` default path)

**JSONB snapshots** (legacy, v1):
- `dashboard_snapshots` table — pre-computed blobs
- Still populated by `pull_data.py` / `api/cron.py`
- Accessible via `/api/snapshot?source=snapshot`

### Data Sync
- **Initial load**: `python3 initial_load.py --days 30` — bulk-fetches all data from Close CRM into normalized tables
- **Incremental sync**: `python3 sync_events.py` — uses Close Event Log API to fetch only new/changed data (~15 API calls vs ~325)
  - GitHub Actions runs every 15 minutes (`.github/workflows/sync-events.yml`)
  - Event Log has 30-day retention; if cursor is stale, re-run `initial_load.py`
  - Requires admin API key (non-admin keys lose Event Log `data` access after 1 hour)

### Legacy Data Pull (still works)
- **GitHub Actions**: `pull_data.py --mtd` runs daily (`.github/workflows/refresh-data.yml`)
- **Vercel serverless**: `/api/cron` state machine for batched processing

### Frontend
- Static site (HTML + vanilla JS), unchanged
- Fetches data via `/api/snapshot` — now defaults to relational query, falls back to JSONB with `?source=snapshot`

## Project Structure
```
pull_data.py                       — fetches from Close, inserts snapshot into Supabase (legacy)
db.py                              — shared upsert/query helpers for normalized tables
dashboard_query.py                 — shared dashboard query logic (relational tables → JSON)
initial_load.py                    — one-time full load from Close to normalized tables
sync_events.py                     — incremental sync via Close Event Log API
requirements.txt                   — Python deps (requests, supabase, python-dotenv)
.github/workflows/refresh-data.yml — GitHub Actions cron for legacy pull (daily)
.github/workflows/sync-events.yml  — GitHub Actions cron for incremental sync (every 15 min)
api/cron.py                        — Vercel serverless batch processor (legacy state machine)
api/dashboard.py                   — Vercel serverless — query relational tables, return JSON
api/snapshot.py                    — returns dashboard JSON (relational default, ?source=snapshot for legacy)
api/status.py                      — returns current cron phase + progress
index.html                         — main dashboard page
css/styles.css                     — all styling
js/app.js                          — reads snapshot via /api/snapshot, renders dashboard
setup.sql                          — Supabase table DDL (dashboard_snapshots + cron_state)
setup_v2.sql                       — Supabase table DDL (normalized tables: v2)
run_cron.sh                        — bash script to run cron loop locally
vercel.json                        — Vercel config (Python runtime, rewrites)
CLAUDE.md                          — this file
```

## Supabase
- Project URL: set via `SUPABASE_URL` env var
- **Normalized tables (v2):**
  - `users(id text PK, name, email, synced_at)`
  - `leads(id text PK, display_name, contact_name, synced_at)`
  - `opportunities(id text PK, lead_id, status_id, status_label, pipeline_id, user_id, created_at, updated_at, synced_at)`
  - `opportunity_status_changes(id text PK, lead_id, opportunity_id, old_status_id, new_status_id, changed_at, user_id, synced_at)`
  - `calls(id text PK, lead_id, user_id, date_created, duration, status, synced_at)`
  - `sync_cursors(entity_type text PK, last_event_date, last_synced_at)`
- **Legacy tables:** `dashboard_snapshots`, `cron_state`
- RLS: writes require secret key. Entity tables have anon read policies. `sync_cursors` is service-key only.
- Secret key lives in GitHub Secrets and Vercel env vars.

## Local Setup

### Initial load (new normalized tables)
```
cp .env.example .env   # then fill in keys
pip install -r requirements.txt
# Run setup_v2.sql in Supabase SQL Editor first
python3 initial_load.py --days 30
```

### Incremental sync
```
python3 sync_events.py
```

### Legacy data pull
```
python3 pull_data.py --days 7
```

## Close API Auth
Uses HTTP Basic auth with API key as username, empty password.
Base URL: `https://api.close.com/api/v1/`

## API Endpoints
- `GET /api/snapshot` — dashboard data (relational query, default)
- `GET /api/snapshot?source=snapshot` — dashboard data (legacy JSONB snapshot)
- `GET /api/dashboard` — dashboard data (relational query, standalone)
- `GET /api/dashboard?start=YYYY-MM-DD&end=YYYY-MM-DD` — custom date range
- `GET /api/cron` — trigger legacy batch processing step
- `GET /api/status` — legacy cron status
