-- OMN-10340: deterministic savings estimate projection table.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS savings_estimates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_timestamp TIMESTAMPTZ NOT NULL,
    session_id TEXT NOT NULL,
    model_local TEXT NOT NULL,
    model_cloud_baseline TEXT NOT NULL,
    local_cost_usd NUMERIC(18, 6) NOT NULL CHECK (local_cost_usd >= 0),
    cloud_cost_usd NUMERIC(18, 6) NOT NULL CHECK (cloud_cost_usd >= 0),
    savings_usd NUMERIC(18, 6) NOT NULL,
    repo_name TEXT,
    machine_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT savings_estimates_amounts_match
        CHECK (savings_usd = cloud_cost_usd - local_cost_usd)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_savings_estimates_identity
    ON savings_estimates (
        session_id,
        event_timestamp,
        model_local,
        model_cloud_baseline
    );

CREATE OR REPLACE FUNCTION refresh_savings_estimates_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_savings_estimates_updated_at ON savings_estimates;
CREATE TRIGGER trg_savings_estimates_updated_at
    BEFORE UPDATE ON savings_estimates
    FOR EACH ROW
    EXECUTE FUNCTION refresh_savings_estimates_updated_at();
