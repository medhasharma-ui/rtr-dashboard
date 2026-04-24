-- ============================================================
-- Speed-to-Call Dashboard v2 — Normalized Tables
-- Run this in Supabase SQL Editor after setup.sql.
-- Safe to re-run (all statements are idempotent).
-- ============================================================


-- 1. users — Close CRM users (AEs)
CREATE TABLE IF NOT EXISTS users (
  id         text        PRIMARY KEY,
  name       text,
  email      text,
  synced_at  timestamptz DEFAULT now()
);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_users" ON users;
CREATE POLICY "anon_select_users"
  ON users FOR SELECT TO anon USING (true);


-- 2. leads — Close CRM leads
CREATE TABLE IF NOT EXISTS leads (
  id            text        PRIMARY KEY,
  display_name  text,
  contact_name  text,
  synced_at     timestamptz DEFAULT now()
);

ALTER TABLE leads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_leads" ON leads;
CREATE POLICY "anon_select_leads"
  ON leads FOR SELECT TO anon USING (true);


-- 3. opportunities — Close CRM opportunities
CREATE TABLE IF NOT EXISTS opportunities (
  id            text        PRIMARY KEY,
  lead_id       text,
  status_id     text,
  status_label  text,
  pipeline_id   text,
  user_id       text,
  created_at    timestamptz,
  updated_at    timestamptz,
  synced_at     timestamptz DEFAULT now()
);

ALTER TABLE opportunities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_opportunities" ON opportunities;
CREATE POLICY "anon_select_opportunities"
  ON opportunities FOR SELECT TO anon USING (true);

CREATE INDEX IF NOT EXISTS idx_opportunities_lead_id
  ON opportunities (lead_id);

CREATE INDEX IF NOT EXISTS idx_opportunities_pipeline_id
  ON opportunities (pipeline_id);


-- 4. opportunity_status_changes — RTR transition events
CREATE TABLE IF NOT EXISTS opportunity_status_changes (
  id               text        PRIMARY KEY,
  lead_id          text,
  opportunity_id   text,
  old_status_id    text,
  new_status_id    text,
  changed_at       timestamptz,
  user_id          text,
  synced_at        timestamptz DEFAULT now()
);

ALTER TABLE opportunity_status_changes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_osc" ON opportunity_status_changes;
CREATE POLICY "anon_select_osc"
  ON opportunity_status_changes FOR SELECT TO anon USING (true);

CREATE INDEX IF NOT EXISTS idx_osc_status_ids
  ON opportunity_status_changes (old_status_id, new_status_id);

CREATE INDEX IF NOT EXISTS idx_osc_lead_id
  ON opportunity_status_changes (lead_id);

CREATE INDEX IF NOT EXISTS idx_osc_changed_at
  ON opportunity_status_changes (changed_at);


-- 5. calls — Close CRM call activities
CREATE TABLE IF NOT EXISTS calls (
  id            text        PRIMARY KEY,
  lead_id       text,
  user_id       text,
  date_created  timestamptz,
  duration      integer     DEFAULT 0,
  status        text,
  synced_at     timestamptz DEFAULT now()
);

ALTER TABLE calls ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_calls" ON calls;
CREATE POLICY "anon_select_calls"
  ON calls FOR SELECT TO anon USING (true);

CREATE INDEX IF NOT EXISTS idx_calls_lead_id
  ON calls (lead_id);

CREATE INDEX IF NOT EXISTS idx_calls_date_created
  ON calls (date_created);


-- 6. sync_cursors — tracks incremental sync position
CREATE TABLE IF NOT EXISTS sync_cursors (
  entity_type    text        PRIMARY KEY,
  last_event_date timestamptz,
  last_synced_at  timestamptz DEFAULT now()
);

ALTER TABLE sync_cursors ENABLE ROW LEVEL SECURITY;
-- No anon policies — only service key can read/write this table.
