-- Security fixes + onboarding agent support (ops #3, #13) — 2026-09-24
-- Idempotent; safe to run on the live Postgres BEFORE deploying the new server.js.

-- Alert outcome: 'delivered' or 'expired' (NULL = pending, or rows from before this migration)
ALTER TABLE pending_alerts ADD COLUMN IF NOT EXISTS outcome text;

-- Delivery-path and webhook-test evidence
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_alert_poll_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_tv_test_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_tv_test_payload text;

-- Onboarding chat transcripts (kept 30 days) and daily usage cap
CREATE TABLE IF NOT EXISTS onboarding_messages (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_onboarding_messages_user ON onboarding_messages(user_id, created_at);

CREATE TABLE IF NOT EXISTS onboarding_usage (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day date NOT NULL,
    turns integer NOT NULL DEFAULT 0,
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- Hand-offs to a human (Grok Bot triages these)
CREATE TABLE IF NOT EXISTS support_tickets (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    email text,
    source text NOT NULL DEFAULT 'onboarding_agent',
    reason text,
    summary text NOT NULL,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at timestamptz NOT NULL DEFAULT now()
);
