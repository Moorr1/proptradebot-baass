-- TradingView webhook relay support — 2026-07-19
-- Adds per-user webhook token + passphrase, and a pending-alerts queue the bot polls.
-- Idempotent; safe to run on the live Render Postgres.

ALTER TABLE users ADD COLUMN IF NOT EXISTS tv_token text UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tv_passphrase text;

CREATE TABLE IF NOT EXISTS pending_alerts (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payload jsonb NOT NULL,
    source text NOT NULL DEFAULT 'tradingview',
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);

-- fast lookup of a user's undelivered alerts
CREATE INDEX IF NOT EXISTS idx_pending_alerts_undelivered
    ON pending_alerts(user_id, created_at) WHERE delivered_at IS NULL;
