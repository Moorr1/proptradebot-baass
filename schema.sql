-- PropTradeBot BaaS Database Schema
-- Run with: psql "${NEON_CONNECTION_STRING}" -f schema.sql

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users table (linked to Clerk)
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    clerk_id text UNIQUE NOT NULL,
    email text UNIQUE NOT NULL,
    name text,
    plan_tier text NOT NULL DEFAULT 'none' CHECK (plan_tier IN ('none', 'basic', 'pro', 'enterprise')),
    subscription_status text NOT NULL DEFAULT 'inactive' CHECK (subscription_status IN ('active', 'inactive', 'past_due', 'cancelled', 'trialing')),
    stripe_customer_id text,
    stripe_subscription_id text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

-- Trading accounts managed by the bot
CREATE TABLE IF NOT EXISTS accounts (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    prop_firm text NOT NULL,
    account_number text NOT NULL,
    platform text NOT NULL DEFAULT 'topstep',
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'blown', 'funded', 'eval')),
    starting_balance numeric(12,2),
    current_balance numeric(12,2),
    daily_loss_limit numeric(12,2),
    max_drawdown numeric(12,2),
    api_key_encrypted text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    UNIQUE(user_id, account_number)
);

-- Stripe events log
CREATE TABLE IF NOT EXISTS stripe_events (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    stripe_event_id text UNIQUE NOT NULL,
    event_type text NOT NULL,
    customer_id text,
    subscription_id text,
    payload jsonb,
    processed_at timestamptz DEFAULT now()
);

-- Trade execution logs
CREATE TABLE IF NOT EXISTS trades (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    account_id uuid REFERENCES accounts(id) ON DELETE CASCADE,
    signal_id text,
    trade_direction text NOT NULL CHECK (trade_direction IN ('long', 'short')),
    symbol text NOT NULL,
    contracts integer NOT NULL,
    entry_price numeric(12,4),
    exit_price numeric(12,4),
    stop_price numeric(12,4),
    target_prices numeric(12,4)[],
    realized_pnl numeric(12,2),
    commission numeric(12,2) DEFAULT 0,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'cancelled')),
    opened_at timestamptz DEFAULT now(),
    closed_at timestamptz,
    metadata jsonb
);

-- Bot configurations per user
CREATE TABLE IF NOT EXISTS bot_configs (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    strategy text NOT NULL DEFAULT 'auto',
    contract_count integer NOT NULL DEFAULT 1,
    stop_loss_ticks integer,
    target_multipliers integer[],
    auto_trading boolean NOT NULL DEFAULT false,
    risk_per_trade numeric(5,4) DEFAULT 0.02,
    max_daily_trades integer DEFAULT 10,
    allowed_symbols text[] DEFAULT '{MES, MNQ}'::text[],
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

-- Performance snapshots (daily summaries)
CREATE TABLE IF NOT EXISTS performance_snapshots (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    account_id uuid REFERENCES accounts(id) ON DELETE CASCADE,
    date date NOT NULL,
    total_trades integer DEFAULT 0,
    winning_trades integer DEFAULT 0,
    losing_trades integer DEFAULT 0,
    gross_pnl numeric(12,2) DEFAULT 0,
    net_pnl numeric(12,2) DEFAULT 0,
    max_drawdown numeric(12,2) DEFAULT 0,
    equity numeric(12,2),
    UNIQUE(user_id, account_id, date)
);

-- Trial fingerprint tracking (prevents trial abuse)
CREATE TABLE IF NOT EXISTS trial_fingerprints (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    fingerprint text NOT NULL,
    fingerprint_type text NOT NULL CHECK (fingerprint_type IN ('card', 'email_domain', 'ip_hash')),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz DEFAULT now(),
    UNIQUE(fingerprint, fingerprint_type)
);

CREATE INDEX IF NOT EXISTS idx_trial_fingerprints_lookup ON trial_fingerprints(fingerprint, fingerprint_type);
CREATE INDEX IF NOT EXISTS idx_users_clerk_id ON users(clerk_id);
CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_user_id ON trades(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_account_id ON trades(account_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_performance_user_date ON performance_snapshots(user_id, date);

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Triggers
DROP TRIGGER IF EXISTS update_users_updated_at ON users;
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_accounts_updated_at ON accounts;
CREATE TRIGGER update_accounts_updated_at BEFORE UPDATE ON accounts FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_bot_configs_updated_at ON bot_configs;
CREATE TRIGGER update_bot_configs_updated_at BEFORE UPDATE ON bot_configs FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
