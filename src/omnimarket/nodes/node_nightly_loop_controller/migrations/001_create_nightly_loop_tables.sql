-- Migration: Create nightly loop controller tables
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_nightly_loop_controller

-- Persistent decision store: every decision the nightly loop makes
CREATE TABLE IF NOT EXISTS nightly_loop_decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id     TEXT UNIQUE NOT NULL,
    iteration_id    TEXT NOT NULL,
    correlation_id  TEXT NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action          TEXT NOT NULL,
    target          TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    model_used      TEXT DEFAULT '',
    cost_usd        NUMERIC(10, 6) DEFAULT 0,
    details         TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nld_correlation_id ON nightly_loop_decisions (correlation_id);
CREATE INDEX IF NOT EXISTS idx_nld_iteration_id ON nightly_loop_decisions (iteration_id);
CREATE INDEX IF NOT EXISTS idx_nld_timestamp ON nightly_loop_decisions (timestamp);
CREATE INDEX IF NOT EXISTS idx_nld_action ON nightly_loop_decisions (action);

-- Iteration history: summary of each loop iteration
CREATE TABLE IF NOT EXISTS nightly_loop_iterations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    iteration_id        TEXT UNIQUE NOT NULL,
    correlation_id      TEXT NOT NULL,
    iteration_number    INT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    gaps_checked        INT DEFAULT 0,
    gaps_closed         INT DEFAULT 0,
    decisions_made      INT DEFAULT 0,
    tickets_dispatched  INT DEFAULT 0,
    total_cost_usd      NUMERIC(10, 6) DEFAULT 0,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_nli_correlation_id ON nightly_loop_iterations (correlation_id);
CREATE INDEX IF NOT EXISTS idx_nli_started_at ON nightly_loop_iterations (started_at);
