-- Web + Agent step 2: versioned ladder settings. Idempotent; run before deploying.
SET lock_timeout = '3s';
CREATE TABLE IF NOT EXISTS settings_versions (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version integer NOT NULL,
    settings jsonb NOT NULL,
    source text NOT NULL CHECK (source IN ('app', 'web')),
    risk_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, version)
);
