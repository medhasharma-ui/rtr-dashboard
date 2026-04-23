-- ============================================================
-- Speed-to-Call Dashboard — Full Database Setup
-- Run this in Supabase SQL Editor for a fresh deployment.
-- Safe to re-run (all statements are idempotent).
-- ============================================================


-- 1. dashboard_snapshots — stores pipeline result snapshots
--    Written by cron (service key), read by frontend (anon key).

CREATE TABLE IF NOT EXISTS dashboard_snapshots (
  id           uuid         DEFAULT gen_random_uuid() PRIMARY KEY,
  generated_at timestamptz  NOT NULL,
  data         jsonb        NOT NULL
);

ALTER TABLE dashboard_snapshots ENABLE ROW LEVEL SECURITY;

-- Allow frontend (anon/publishable key) to read snapshots
DROP POLICY IF EXISTS "anon_select_snapshots" ON dashboard_snapshots;
CREATE POLICY "anon_select_snapshots"
  ON dashboard_snapshots FOR SELECT
  TO anon
  USING (true);


-- 2. cron_state — tracks batch processing progress
--    One row per mode: 'mtd' and 'recent'. Only accessed by service key.

CREATE TABLE IF NOT EXISTS cron_state (
  id          text        PRIMARY KEY,
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
  range_type  text        DEFAULT 'mtd',
  dry         boolean     DEFAULT false,
  retries     int         NOT NULL DEFAULT 0,
  started_at  timestamptz,
  updated_at  timestamptz
);

ALTER TABLE cron_state ENABLE ROW LEVEL SECURITY;
-- No anon policies — only service key can read/write this table.

-- 3. Migrations for existing deployments
--    These are no-ops on a fresh install (columns already exist).
ALTER TABLE cron_state ADD COLUMN IF NOT EXISTS retries int NOT NULL DEFAULT 0;
ALTER TABLE cron_state ADD COLUMN IF NOT EXISTS range_type text DEFAULT 'mtd';

-- Seed / migrate to per-mode rows (idempotent)
INSERT INTO cron_state (id, range_type) VALUES ('mtd', 'mtd')
ON CONFLICT (id) DO NOTHING;
INSERT INTO cron_state (id, range_type) VALUES ('recent', 'recent')
ON CONFLICT (id) DO NOTHING;
DELETE FROM cron_state WHERE id = 'current';
