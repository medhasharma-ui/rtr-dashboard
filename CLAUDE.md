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

## Project Structure
```
pull_data.py        — fetches data from Close API, outputs data/dashboard_data.json
index.html          — main dashboard page
css/styles.css      — all styling
js/app.js           — dashboard logic (filtering, sorting, rendering)
data/               — generated JSON data files
CLAUDE.md           — this file
```

## How to run
1. Create `.env` file with your Close API key:
   ```
   echo 'CLOSE_API_KEY=your_key_here' > .env
   ```
2. Run the startup script:
   ```
   ./start.sh
   ```
3. Open http://rtr-dashboard.local:8080

The server pulls live data from Close on startup (last 7 days) and serves the dashboard.
Click "Refresh Data" button on the dashboard to re-pull from Close.

## Manual data pull (without server)
```
export CLOSE_API_KEY=your_key_here
python3 pull_data.py --days 7
```

## Close API Auth
Uses HTTP Basic auth with API key as username, empty password.
Base URL: `https://api.close.com/api/v1/`
