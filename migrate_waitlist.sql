-- Waitlist while sales are closed (ops #4) — 2026-09-27
-- Idempotent; safe to run on the live Postgres BEFORE deploying the new server.js.
-- A new table only, no locks on existing tables.
SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS waitlist (
    id bigserial PRIMARY KEY,
    email text NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) <= 254),
    handle text CHECK (handle IS NULL OR length(handle) <= 100),
    source text,
    created_at timestamptz NOT NULL DEFAULT now(),
    invited_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_waitlist_created ON waitlist(created_at);
