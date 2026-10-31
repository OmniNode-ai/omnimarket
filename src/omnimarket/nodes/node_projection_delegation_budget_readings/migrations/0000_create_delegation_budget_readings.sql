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

BEGIN;

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

-- Reconcile empty or compatible tables to the declared constraints. A populated
-- table missing required producer values must fail rather than invent events.
-- The transaction leaves no partial repair behind on such a refusal.
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS correlation_id             UUID NOT NULL;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS status                     TEXT NOT NULL;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS task_type                  TEXT NOT NULL;
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
    ADD COLUMN IF NOT EXISTS observed_at                TIMESTAMPTZ NOT NULL;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS projected_at               TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Do not expand BIGSERIAL on each guarded ADD: that can allocate unused
-- sequences even when the column already exists. Keep one owned sequence.
CREATE SEQUENCE IF NOT EXISTS omninode_internal.delegation_budget_readings_projection_cursor_seq;
ALTER TABLE omninode_internal.delegation_budget_readings
    ADD COLUMN IF NOT EXISTS projection_cursor BIGINT NOT NULL
        DEFAULT nextval('omninode_internal.delegation_budget_readings_projection_cursor_seq'::regclass);
ALTER SEQUENCE omninode_internal.delegation_budget_readings_projection_cursor_seq
    OWNED BY omninode_internal.delegation_budget_readings.projection_cursor;

-- ADD IF NOT EXISTS does not strengthen columns that are already present.
ALTER TABLE omninode_internal.delegation_budget_readings
    ALTER COLUMN correlation_id SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN task_type SET NOT NULL,
    ALTER COLUMN observed_at SET NOT NULL,
    ALTER COLUMN projected_at SET NOT NULL,
    ALTER COLUMN projected_at SET DEFAULT NOW(),
    ALTER COLUMN projection_cursor SET NOT NULL,
    ALTER COLUMN projection_cursor SET DEFAULT
        nextval('omninode_internal.delegation_budget_readings_projection_cursor_seq'::regclass);

DO $constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.delegation_budget_readings'::regclass
          AND conname = 'pk_delegation_budget_readings'
    ) THEN
        ALTER TABLE omninode_internal.delegation_budget_readings
            ADD CONSTRAINT pk_delegation_budget_readings PRIMARY KEY (correlation_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'omninode_internal.delegation_budget_readings'::regclass
          AND conname = 'ck_delegation_budget_readings_status'
    ) THEN
        ALTER TABLE omninode_internal.delegation_budget_readings
            ADD CONSTRAINT ck_delegation_budget_readings_status
                CHECK (status IN ('completed', 'failed', 'timeout'));
    END IF;
END
$constraints$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_budget_readings_projection_cursor
    ON omninode_internal.delegation_budget_readings (projection_cursor);

-- The question the AC3 readback asks: which terminals were observed in this window, by status?
CREATE INDEX IF NOT EXISTS idx_delegation_budget_readings_observed_at
    ON omninode_internal.delegation_budget_readings (observed_at, status);

COMMENT ON TABLE omninode_internal.delegation_budget_readings IS
    'OMN-21326 AC3: one row per delegate-skill terminal with the budget the '
    'run resolved and the handler duration. Written only by '
    'node_projection_delegation_budget_readings.';

COMMIT;
