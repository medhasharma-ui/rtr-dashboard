# Implementation Details

## Overview

Speed-to-Call Dashboard for FlyHomes. Tracks whether AEs called a lead within 2 hours of an opportunity moving from **Ready to Respond** to **Active Scenario** / **Declined Scenario** / **Addl Info Needed** in Close CRM.

---

## Architecture

```
                          every 6 hrs (cron-job.org)
                                  |
                                  v
                      +-----------------------+
                      |   /api/cron?reset=1   |  Vercel Serverless (Python)
                      +-----------+-----------+
                                  |
              every minute  <-----+-----> /api/status
              (cron-job.org)      |
                                  v
                      +-----------------------+
                      |      /api/cron        |  processes next step
                      +-----------+-----------+
                           |             |
              Close CRM API|             | Supabase
              (read leads, |             | (read/write cron_state,
               calls, etc) |             |  write dashboard_snapshots)
                           v             v
                    +-----------+  +---------------+
                    | Close CRM |  |   Supabase    |
                    +-----------+  +-------+-------+
                                           |
                                           | PostgREST (public key, read-only)
                                           v
                                   +---------------+
                                   |  Browser UI   |
                                   | index.html    |
                                   | js/app.js     |
                                   +---------------+
```

**Components:**
- **Vercel** - Hosts static frontend + Python serverless functions
- **Supabase** - PostgreSQL database (snapshots + cron state)
- **Close CRM** - Source of truth for leads, opportunities, calls
- **cron-job.org** - External scheduler (Vercel Hobby limits crons to 1/day)

---

## Project Structure

```
rtr-dashboard/
|-- index.html                     Main dashboard page
|-- css/styles.css                 All styling
|-- js/app.js                      Reads snapshot from Supabase, renders UI
|
|-- pull_data.py                   Core data pipeline (shared by CLI + serverless)
|-- api/
|   |-- cron.py                    Serverless batch processor (state machine)
|   +-- status.py                  Returns current cron phase + progress
|
|-- setup.sql                      Supabase table DDL (cron_state)
|-- run_cron.sh                    Bash script to run cron loop locally
|-- requirements.txt               Python deps: requests, python-dotenv, supabase
|-- vercel.json                    Vercel config (Python runtime, cron, rewrites)
|-- .github/workflows/
|   +-- refresh-data.yml           GitHub Actions cron (legacy, replaced by Vercel)
+-- .env.example                   Env var template
```

---

## Data Pipeline (`pull_data.py`)

### Close CRM IDs

| Entity | ID |
|--------|----|
| Pipeline (REQ) | `pipe_5VzsEaw8Df23USMhIwmMfz` |
| Ready to Respond | `stat_AZ0tc4F8UzLQJyVG9vLH23R5RpMnIZdUkiNH7xvvVeb` |
| Active Scenario | `stat_Pn5zo8keGKa8rK4QCbg1sQAt72vREwPDPcZ9MyXv9Wf` |
| Declined Scenario | `stat_ES08dw9Ij4gVsMrcuCtmviVwlJ0COaYJIrUgJtBWEtk` |
| Addl Info Needed | `stat_RIXpsfGd3QDdTzdYQU16XLVaj1M6h4X6JV8qsZ8d7tW` |

### Classification Logic

For each RTR transition found:

1. Find the **earliest call** on that lead **after** the status change timestamp
2. Classify:

| Bucket | Condition |
|--------|-----------|
| `within` | Call found, `call_time - change_time <= 120 min` |
| `after` | Call found, `call_time - change_time > 120 min` |
| `never` | No call found, and `now - change_time > 120 min` |
| `pending` | No call found, but `now - change_time <= 120 min` (still in SLA window) |

### Key Functions

| Function | What it does |
|----------|-------------|
| `close_get()` | HTTP GET to Close API with Basic auth + retry on 429 |
| `fetch_lead_ids()` | Get unique lead IDs from updated opportunities in REQ pipeline |
| `fetch_all_calls_bulk()` | Bulk-fetch all calls in date range, returns `{lead_id: [timestamps]}` |
| `fetch_calls_chunk()` | Fetch N pages of calls speculatively (used by serverless incremental fetch) |
| `fetch_status_changes_for_lead()` | Get RTR transitions for a single lead |
| `fetch_transitions_parallel()` | Parallel wrapper over `fetch_status_changes_for_lead` |
| `fetch_lead_infos_parallel()` | Parallel fetch of lead display names + contacts |
| `fetch_users()` | Get `{user_id: name}` mapping |
| `process_transitions()` | Classify all transitions using pre-fetched bulk data |
| `build_snapshot()` | Build the final JSON snapshot (grouped by Pacific-time date) |

### Parallelization Strategy

**Problem:** Close API returns `total_results` for the opportunity endpoint but NOT for the activity/call endpoint. Without knowing the total, you can't fire all pages in parallel upfront.

**Solution:** `_fetch_all_pages_parallel()` uses two strategies:

1. **Known total** (opportunities): Fetch page 0, read `total_results`, fire all remaining pages in parallel (5 workers).
2. **Unknown total** (calls): **Speculative parallel fetch** - fire 50 pages at once (skip 100 to 5000), collect results in order, stop at the first page with < 100 rows. Falls back to sequential if > 5000 items.

**Rate limiting:** Close API rate limit is ~40 RPS. Workers are capped at 5-6 per pool to stay under the limit. `close_get()` retries on 429 with the `Retry-After` header.

### CLI Usage

```bash
# Pull last 7 days and insert snapshot
python3 pull_data.py --days 7

# Pull specific date range
python3 pull_data.py --start 2026-04-01 --end 2026-04-15
```

---

## Serverless Cron (`api/cron.py`)

### Why batching?

Vercel serverless functions have execution time limits (10s Hobby, 60s Pro). A full data pull takes ~90s. The solution: break the work into steps, persist state in Supabase between invocations, and let an external cron hit the endpoint repeatedly.

### State Machine

```
idle ──> init_leads ──> init_calls ──> processing ──> complete
          (4s)        (5s x 2-3)     (8s x ~20)      (0.3s)
```

Each arrow = one HTTP request to `/api/cron`.

### Phase Details

#### `init_leads` (~4s)
- Fetches lead IDs + users in parallel (2 threads)
- Saves to `cron_state` with phase=`init_calls`
- Stores initial `bulk_calls = {"_rows": [], "_skip": 0}` for incremental call fetching

#### `init_calls` (~5s per step, 2-3 steps)
- Reads `_skip` and `_rows` from `bulk_calls` in state
- Calls `fetch_calls_chunk()` with 20 pages of speculative parallel fetch
- If more pages exist: saves partial `_rows` and updated `_skip`, stays in `init_calls`
- If done: converts rows to `{lead_id: [timestamps]}` dict, transitions to `processing`

#### `processing` (~8s per batch, ~20 batches)
- Processes `BATCH_SIZE` (default 30) leads per invocation
- For each batch:
  1. Fetch status changes for all batch leads (6 parallel workers)
  2. Fetch lead info for leads with transitions (10 parallel workers)
  3. Classify using pre-fetched bulk calls
  4. Append results, advance cursor, save state

#### `complete`
- `do_finalize()` builds the snapshot JSON and inserts into `dashboard_snapshots`
- Clears large fields from `cron_state` to keep the row small

### Crash Recovery

Every phase is designed so that if Vercel kills the function mid-execution, the next invocation picks up correctly:

| Phase | If killed mid-execution | Recovery |
|-------|------------------------|----------|
| `init_leads` | State not saved yet | Re-runs from scratch |
| `init_calls` | `_skip` not updated | Refetches same chunk (idempotent) |
| `processing` | Cursor not advanced | Retries same batch |
| `finalize` | Snapshot may not insert | Retries finalize |

**Stuck batch protection:** Before each batch, the `retries` counter is incremented via a lightweight partial DB update. On success, it resets to 0. After 3 consecutive failures on the same cursor (indicating the batch consistently times out), the batch is **skipped** and processing continues.

### Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `CRON_BATCH_SIZE` | 30 | Leads per processing batch |
| `CALLS_PAGES_PER_STEP` | 20 | Speculative pages per init_calls step |
| `CLOSE_API_KEY` | (required) | Close CRM API key |
| `SUPABASE_URL` | (required) | Supabase project URL |
| `SUPABASE_SECRET_KEY` | (required) | Supabase service role key |

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cron` | GET | Process next step (or start new run if idle/complete) |
| `/api/cron?reset=1` | GET | Force-reset and start a fresh run |
| `/api/cron?dry=1` | GET | Dry-run mode (no snapshot insert) |
| `/api/cron?reset=1&dry=1` | GET | Fresh dry run |
| `/api/status` | GET | Current phase, cursor, total, timestamps |

### Dry Run

Pass `?dry=1` to skip the `dashboard_snapshots` insert. The snapshot JSON is returned in the final response instead. Useful for local testing without polluting the production database.

---

## Supabase Schema

### `dashboard_snapshots`

```sql
CREATE TABLE dashboard_snapshots (
  id           uuid         DEFAULT gen_random_uuid() PRIMARY KEY,
  generated_at timestamptz  NOT NULL,
  data         jsonb        NOT NULL
);
-- RLS: SELECT for anon key, INSERT/UPDATE for service key only
```

The `data` JSONB column contains:
```json
{
  "generated_at": "2026-04-23T...",
  "start_date": "2026-04-16",
  "end_date": "2026-04-23",
  "total_leads": 355,
  "by_date": {
    "2026-04-16": [ ... results ... ],
    "2026-04-17": [ ... results ... ]
  },
  "all": [ ... all results ... ]
}
```

Each result:
```json
{
  "contact": "John Doe",
  "ae": "Jane Smith",
  "changedAt": "2026-04-20T14:30:00+00:00",
  "callAt": "2026-04-20T15:10:00+00:00",
  "minsToCall": 40.0,
  "bucket": "within",
  "leadId": "lead_xxx",
  "opportunityId": "oppo_xxx",
  "transition": "Active Scenario"
}
```

### `cron_state`

```sql
CREATE TABLE cron_state (
  id          text        PRIMARY KEY DEFAULT 'current',
  phase       text        NOT NULL DEFAULT 'idle',
  cursor      int         NOT NULL DEFAULT 0,
  total       int         NOT NULL DEFAULT 0,
  lead_ids    jsonb       DEFAULT '[]'::jsonb,
  bulk_calls  jsonb       DEFAULT '{}'::jsonb,
  users       jsonb       DEFAULT '{}'::jsonb,
  results     jsonb       DEFAULT '[]'::jsonb,
  start_date  text,
  end_date    text,
  api_end     text,
  dry         boolean     DEFAULT false,
  retries     int         NOT NULL DEFAULT 0,
  started_at  timestamptz,
  updated_at  timestamptz
);
```

Single row (`id='current'`). RLS enabled, no anon policies (service key only).

---

## External Cron Setup (cron-job.org)

Two cron jobs needed:

### Job 1: Reset (every 6 hours)
- **URL:** `https://your-app.vercel.app/api/cron?reset=1`
- **Schedule:** `0 */6 * * *`
- **Purpose:** Starts a fresh data pull

### Job 2: Process (every minute)
- **URL:** `https://your-app.vercel.app/api/cron`
- **Schedule:** `* * * * *`
- **Purpose:** Processes the next step. When phase is `complete`, this is a no-op.

The minute-cron keeps hitting `/api/cron` which processes one step each time. Once all steps are done (phase=`complete`), subsequent hits return immediately with a "Run already complete" message until the next 6-hour reset.

---

## Frontend (`js/app.js`)

- Connects to Supabase using the **publishable (anon) key** (safe to embed)
- Fetches the latest row from `dashboard_snapshots` ordered by `generated_at DESC`
- Renders four classification cards with counts and percentages
- Date tabs: Today, Yesterday, and one tab per date in the snapshot
- AE multi-select filter
- Timestamps displayed in Pacific Time
- All rendering is client-side, no build step

---

## Local Development

### Prerequisites
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in CLOSE_API_KEY, SUPABASE_URL, SUPABASE_SECRET_KEY
```

### Run CLI (one-shot, full pull)
```bash
python3 pull_data.py --days 7
```

### Run serverless locally (batched, via Vercel dev)
```bash
vercel dev                                        # start local server
./run_cron.sh http://localhost:3000 --dry-run      # trigger cron loop (no DB insert)
./run_cron.sh http://localhost:3000                # trigger cron loop (inserts snapshot)
```

### Run setup SQL (one-time)
Run `setup.sql` in the Supabase SQL Editor to create the `cron_state` table.

---

## Performance

Measured with ~600 leads and ~4100 calls (7-day window):

| Step | Duration | Notes |
|------|----------|-------|
| `init_leads` | ~4s | Parallel: lead IDs + users |
| `init_calls` x2-3 | ~5s each | 20 speculative pages per step |
| `processing` x20 | ~8s each | 30 leads/batch, 6 workers |
| `finalize` | ~0.3s | Insert snapshot |
| **Total wall time** | **~3 min** | With 1s sleep between steps |

### Optimization techniques used
- **Speculative parallel pagination** for Close API endpoints that don't return `total_results`
- **Bulk call pre-fetch** instead of per-lead call lookups
- **Parallel ThreadPoolExecutor** for status changes, lead info, and pagination
- **Worker caps** (5-6 per pool) to avoid Close API rate limits (429s)
- **429 retry** with `Retry-After` header in `close_get()`
