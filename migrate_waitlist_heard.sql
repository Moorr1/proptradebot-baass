-- Waitlist: where did the signup hear about us? (ops #39) — 2026-10-01
-- Idempotent and additive: one nullable column, no rewrite of existing rows.
SET lock_timeout = '3s';
ALTER TABLE waitlist ADD COLUMN IF NOT EXISTS heard text CHECK (heard IS NULL OR length(heard) <= 40);
