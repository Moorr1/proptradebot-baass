-- Web + Agent step 1: live status and events from the customer's Agent.
-- Idempotent. Run BEFORE deploying the server that uses it.
SET lock_timeout = '3s';

CREATE TABLE IF NOT EXISTS agent_status (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_events (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts timestamptz NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_events_user_ts ON agent_events(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_ts ON agent_events(ts);
