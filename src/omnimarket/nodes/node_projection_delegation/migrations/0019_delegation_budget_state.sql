-- OMN-13235: per-tenant ceiling budget-state surface (cap + consumption),
-- event-sourced from delegation-completed events, projection-readable.
--
-- A `budgeted` ceiling tier (EnumTierCostType.BUDGETED, OMN-13234) carries a
-- monthly_cap_usd: tokens served while headroom remains cost 0 cash and draw the
-- cap down; tokens past the cap bill overage. This table is the durable,
-- event-sourced state of that drawdown so the dashboard / API can show how much
-- of a tenant's monthly ceiling budget is consumed and how much headroom remains.
--
-- One row per (tenant_id, cost_tier_name, budget_period) — the period is the
-- UTC month (YYYY-MM) the cap applies to. Each delegation-completed event for a
-- budgeted tier accumulates its measured headroom drawdown
-- (budget_headroom_consumed_usd, 0018) plus any cash overage into the period
-- row; headroom_remaining_usd is derived = cap - consumed (floored at 0). The
-- write is idempotent per source event via last_correlation_id so a replayed
-- event does not double-count.
--
-- Idempotent CREATE/ADD so warm dev/stability volumes reconcile cleanly.

CREATE TABLE IF NOT EXISTS delegation_budget_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    cost_tier_name TEXT NOT NULL,
    budget_period TEXT NOT NULL,
    monthly_cap_usd NUMERIC(18, 6) NOT NULL CHECK (monthly_cap_usd >= 0),
    consumed_usd NUMERIC(18, 6) NOT NULL DEFAULT 0 CHECK (consumed_usd >= 0),
    overage_usd NUMERIC(18, 6) NOT NULL DEFAULT 0 CHECK (overage_usd >= 0),
    headroom_remaining_usd NUMERIC(18, 6) NOT NULL DEFAULT 0
        CHECK (headroom_remaining_usd >= 0),
    delegation_count INTEGER NOT NULL DEFAULT 0 CHECK (delegation_count >= 0),
    last_correlation_id TEXT,
    first_event_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_event_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_budget_state ----
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

ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS cost_tier_name TEXT;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS budget_period TEXT;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS monthly_cap_usd NUMERIC(18, 6);
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS consumed_usd NUMERIC(18, 6) DEFAULT 0;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS overage_usd NUMERIC(18, 6) DEFAULT 0;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS headroom_remaining_usd NUMERIC(18, 6) DEFAULT 0;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS delegation_count INTEGER DEFAULT 0;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS last_correlation_id TEXT;
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS first_event_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE delegation_budget_state ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'tenant_id', 'cost_tier_name', 'budget_period', 'monthly_cap_usd', 'consumed_usd', 'overage_usd', 'headroom_remaining_usd', 'delegation_count', 'first_event_at', 'last_event_at', 'created_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'delegation_budget_state'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'delegation_budget_state'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge delegation_budget_state.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_budget_state'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE delegation_budget_state ADD CONSTRAINT delegation_budget_state_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_budget_state'::regclass AND conname = 'delegation_budget_state_monthly_cap_usd_check'
    ) THEN
        ALTER TABLE delegation_budget_state ADD CONSTRAINT delegation_budget_state_monthly_cap_usd_check CHECK (monthly_cap_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_budget_state'::regclass AND conname = 'delegation_budget_state_consumed_usd_check'
    ) THEN
        ALTER TABLE delegation_budget_state ADD CONSTRAINT delegation_budget_state_consumed_usd_check CHECK (consumed_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_budget_state'::regclass AND conname = 'delegation_budget_state_overage_usd_check'
    ) THEN
        ALTER TABLE delegation_budget_state ADD CONSTRAINT delegation_budget_state_overage_usd_check CHECK (overage_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_budget_state'::regclass AND conname = 'delegation_budget_state_headroom_remaining_usd_check'
    ) THEN
        ALTER TABLE delegation_budget_state ADD CONSTRAINT delegation_budget_state_headroom_remaining_usd_check CHECK (headroom_remaining_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'delegation_budget_state'::regclass AND conname = 'delegation_budget_state_delegation_count_check'
    ) THEN
        ALTER TABLE delegation_budget_state ADD CONSTRAINT delegation_budget_state_delegation_count_check CHECK (delegation_count >= 0);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: delegation_budget_state ----


CREATE UNIQUE INDEX IF NOT EXISTS ux_delegation_budget_state_identity
    ON delegation_budget_state (tenant_id, cost_tier_name, budget_period);

CREATE OR REPLACE FUNCTION refresh_delegation_budget_state_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_delegation_budget_state_updated_at
    ON delegation_budget_state;
CREATE TRIGGER trg_delegation_budget_state_updated_at
    BEFORE UPDATE ON delegation_budget_state
    FOR EACH ROW
    EXECUTE FUNCTION refresh_delegation_budget_state_updated_at();
