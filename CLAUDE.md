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
- `pull_data.py` runs on a GitHub Actions cron (every 6 hours). It fetches from Close CRM and inserts a snapshot row into Supabase (`dashboard_snapshots` table, JSONB column).
- The frontend is a static site (HTML + vanilla JS) deployed to Vercel. It fetches the latest snapshot directly from Supabase via PostgREST using the public publishable key.
- No server, no long-running process. Read with publishable key (RLS-restricted to SELECT). Write with secret key (CI only).

## Project Structure
```
pull_data.py                       — fetches from Close, inserts snapshot into Supabase
requirements.txt                   — Python deps (requests, supabase, python-dotenv)
.github/workflows/refresh-data.yml — cron schedule for pull_data.py
index.html                         — main dashboard page
css/styles.css                     — all styling
js/app.js                          — reads snapshot from Supabase, renders dashboard
vercel.json                        — Vercel static deploy config
CLAUDE.md                          — this file
```

## Supabase
- Project URL: `https://ercbzutulfrerwmkndhy.supabase.co`
- Table: `dashboard_snapshots(id uuid, generated_at timestamptz, data jsonb)`
- RLS: `select` allowed for anon/publishable key; writes require secret key.
- Frontend embeds the publishable key (safe). Secret key lives only in GitHub Secrets.

## Local data pull
```
cp .env.example .env   # then fill in keys
pip install -r requirements.txt
python3 pull_data.py --days 7
```
Reload the browser to see the new snapshot.

## Close API Auth
Uses HTTP Basic auth with API key as username, empty password.
Base URL: `https://api.close.com/api/v1/`
