-- OMN-21326 AC3 (OMN-17427): one row per delegate-skill terminal.
-- Owner: omnimarket.nodes.node_projection_delegation_budget_readings
-- Target database: omnidash_analytics; physical schema: omninode_internal.
--
-- Key: correlation_id. A later terminal of the same id replaces the row,
-- guarded on observed_at (the producer's envelope time) in the writer's SQL;
-- the history stays on the delegate-skill terminal topics.
--
-- NULL means the terminal did not say. A run with no budget_evidence has NULL
-- timeouts; a terminal that did not measure its handler has a NULL duration.
-- Whether a run outlived its budget plus grace is the readback's verdict
-- (handler_budget_readback) and is not stored.

CREATE TABLE IF NOT EXISTS omninode_internal.delegation_budget_readings (
    correlation_id             UUID        NOT NULL,
    status                     TEXT        NOT NULL,
    task_type                  TEXT        NOT NULL,
    terminal_topic             TEXT,
    requested_timeout_seconds  INTEGER,
    execution_timeout_seconds  INTEGER,
    execution_duration_ms      INTEGER,
    queue_wait_ms              INTEGER,
    caller_lane                TEXT,
    ticket_id                  TEXT,
    observed_at                TIMESTAMPTZ NOT NULL,
    projected_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor          BIGSERIAL   NOT NULL,

    CONSTRAINT pk_delegation_budget_readings PRIMARY KEY (correlation_id),
    CONSTRAINT ck_delegation_budget_readings_status
        CHECK (status IN ('completed', 'failed', 'timeout'))
);

-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so
-- CREATE TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS correlation_id             UUID;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS status                     TEXT;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS task_type                  TEXT;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS terminal_topic             TEXT;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS requested_timeout_seconds  INTEGER;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS execution_timeout_seconds  INTEGER;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS execution_duration_ms      INTEGER;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS queue_wait_ms              INTEGER;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS caller_lane                TEXT;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS ticket_id                  TEXT;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS observed_at                TIMESTAMPTZ;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS projected_at               TIMESTAMPTZ;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS projection_cursor          BIGSERIAL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_budget_readings_projection_cursor
    ON omninode_internal.delegation_budget_readings (projection_cursor);

-- The question the AC3 readback asks: which completed terminals ended in this window?
CREATE INDEX IF NOT EXISTS idx_delegation_budget_readings_observed_at
    ON omninode_internal.delegation_budget_readings (observed_at, status);

COMMENT ON TABLE omninode_internal.delegation_budget_readings IS
    'OMN-21326 AC3: one row per delegate-skill terminal with the budget the '
    'run resolved and the handler duration. Written only by '
    'node_projection_delegation_budget_readings.';
