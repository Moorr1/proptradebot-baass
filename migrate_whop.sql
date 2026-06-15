-- Whop migration
-- Run this once against your Neon DB

-- Add Whop identity columns to users table
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS whop_membership_id TEXT UNIQUE,
  ADD COLUMN IF NOT EXISTS whop_user_id       TEXT,
  ADD COLUMN IF NOT EXISTS billing_source      TEXT NOT NULL DEFAULT 'stripe';
  -- billing_source: 'stripe' | 'whop'

-- Index for fast webhook lookups
CREATE INDEX IF NOT EXISTS idx_users_whop_membership ON users (whop_membership_id);
CREATE INDEX IF NOT EXISTS idx_users_whop_user       ON users (whop_user_id);

-- Whop webhook event log (mirrors stripe_events table)
CREATE TABLE IF NOT EXISTS whop_events (
  id              SERIAL PRIMARY KEY,
  whop_event_id   TEXT UNIQUE NOT NULL,
  event_type      TEXT NOT NULL,
  membership_id   TEXT,
  user_id         TEXT,
  payload         JSONB,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Update existing Stripe users so billing_source is explicit
UPDATE users SET billing_source = 'stripe' WHERE billing_source IS NULL OR billing_source = '';
