-- OMN-13085: sandbox_decisions projection table.
-- Target DB: omnidash_analytics (omnibase_infra postgres on .201:5436)
-- Node: node_projection_sandbox_decisions
-- Append-only: conflict key = correlation_id (INSERT ... ON CONFLICT DO NOTHING)
-- Source: onex.evt.omnimarket.generated-node-invoked.v1

CREATE TABLE IF NOT EXISTS sandbox_decisions (
    correlation_id   TEXT PRIMARY KEY,
    node_name        TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    runtime_backend  TEXT NOT NULL DEFAULT 'sandbox'
                     CHECK (runtime_backend IN ('sandbox', 'runtime')),
    hot_load         BOOLEAN NOT NULL DEFAULT FALSE,
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sandbox_decisions_created_at
    ON sandbox_decisions (created_at);

CREATE INDEX IF NOT EXISTS idx_sandbox_decisions_status
    ON sandbox_decisions (status);

CREATE INDEX IF NOT EXISTS idx_sandbox_decisions_node_name
    ON sandbox_decisions (node_name);
