-- Run this in Supabase SQL Editor to create the cron state table.
-- This table stores the cursor/progress for the batched cron job.
-- Only one row (id='current') is used.

CREATE TABLE IF NOT EXISTS cron_state (
  id          text        PRIMARY KEY DEFAULT 'current',
  phase       text        NOT NULL DEFAULT 'idle',   -- idle | init_calls | processing | complete
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

-- If upgrading an existing table, add the retries column:
ALTER TABLE cron_state ADD COLUMN IF NOT EXISTS retries int NOT NULL DEFAULT 0;

-- Seed the initial row
INSERT INTO cron_state (id) VALUES ('current')
ON CONFLICT (id) DO NOTHING;

-- Enable RLS — no anon policies means only the secret key can access this table.
-- The frontend never touches this table; only the serverless functions do.
ALTER TABLE cron_state ENABLE ROW LEVEL SECURITY;
