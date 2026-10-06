-- OMN-20613 (agent-payments 402 plan, T0.6): make the ceiling budget-state
-- write atomic and replay-safe.
--
-- The writers of delegation_budget_state read the period row, added the event's
-- drawdown in the process, and wrote the sum back. Two concurrent writers both
-- read the same total and one drawdown was lost; a replay of event A after
-- event B double-counted because the only guard was last_correlation_id (the
-- LAST event, so A-B-A applied A twice); and last_event_at was overwritten by
-- whichever event arrived last, not the one that happened last.
--
-- This table records each applied event's identity. The writer inserts the
-- identity first and, only when that insert took a row, runs ONE
-- INSERT ... ON CONFLICT DO UPDATE on delegation_budget_state that increments
-- the totals inside the database, recomputes headroom, and keeps the greater
-- last_event_at. Both statements run in one transaction, so a failure rolls
-- the identity back with the totals.
--
-- The row-level-security step is split into 0057 (fenced on arrival, like the
-- other FORCE RLS steps); this file creates the table and grants the writer.
--
-- Idempotent CREATE so warm dev/stability volumes reconcile cleanly.

CREATE TABLE IF NOT EXISTS delegation_budget_applied_events (
    tenant_id TEXT NOT NULL,
    cost_tier_name TEXT NOT NULL,
    budget_period TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, cost_tier_name, budget_period, correlation_id)
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_budget_applied_events ----
-- The CREATE TABLE IF NOT EXISTS above SILENTLY NO-OPS when a table of this
-- name already exists with a DIFFERENT shape (an out-of-band or legacy apply
-- that predates this migration). Everything below it in this file is NOT so
-- forgiving: CREATE INDEX IF NOT EXISTS guards the index NAME, not the COLUMN,
-- so the first column-dependent statement raises
--   ERROR: column "<col>" does not exist
-- and ON_ERROR_STOP=1 kills the whole migration Job there. Because the runner
-- halts at the first failure, instances of this class surface strictly one per
-- deploy cycle -- OMN-15376 (llm_cost_aggregates.aggregation_key, run
-- 30418878385) and OMN-15302 (baselines_comparisons.snapshot_id) each cost one.
--
-- The guarded adds below converge a drifted pre-existing table onto the shape
-- declared above. On the fresh-create path every one is a no-op (the column
-- already exists), so BOTH paths end at the same schema. No DROP, no recreate,
-- no TRUNCATE: pre-existing rows are preserved. A column that cannot be made
-- NOT NULL without inventing data fails LOUD and names the exact conflict
-- instead of guessing.
--
-- Gated by tests/ci/test_node_migration_shape_reconciliation.py (static) and
-- tests/integration/migrations/test_node_migration_shape_drift_omn15376.py
-- (RED/GREEN + fresh-vs-drifted schema equality on real Postgres).

ALTER TABLE delegation_budget_applied_events ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE delegation_budget_applied_events ADD COLUMN IF NOT EXISTS cost_tier_name TEXT;
ALTER TABLE delegation_budget_applied_events ADD COLUMN IF NOT EXISTS budget_period TEXT;
ALTER TABLE delegation_budget_applied_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE delegation_budget_applied_events ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['tenant_id', 'cost_tier_name', 'budget_period', 'correlation_id', 'applied_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'delegation_budget_applied_events'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'delegation_budget_applied_events'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge delegation_budget_applied_events.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_budget_applied_events'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE delegation_budget_applied_events ADD CONSTRAINT delegation_budget_applied_events_pkey PRIMARY KEY (tenant_id, cost_tier_name, budget_period, correlation_id);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: delegation_budget_applied_events ----

DO $require_tenant_projection_writer$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'tenant_projection_writer'
    ) THEN
        RAISE EXCEPTION
            'tenant_projection_writer role missing; apply flat migration 103 before node migrations';
    END IF;
END
$require_tenant_projection_writer$;

GRANT USAGE ON SCHEMA public TO tenant_projection_writer;
GRANT SELECT, INSERT, UPDATE ON delegation_budget_applied_events TO tenant_projection_writer;
