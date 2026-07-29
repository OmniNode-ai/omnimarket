-- OMN-13131 / W5: Renderer Capability Registry projection.
-- Sole writer: node_renderer_capability_projection (NodeReducer). This table IS
-- the registry — there is no in-memory CapabilityRegistry class. Consumers read
-- the materialized projection via GET /projection/onex.evt.omnimarket.renderer-capability-projection-snapshot.v1.
-- One row per renderer_id (UPSERT key); heartbeat-TTL freshness is materialized
-- as is_degraded + a typed empty_state_reason ('upstream-blocked' when degraded).

CREATE TABLE IF NOT EXISTS renderer_capability_projection (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Upsert key — one row per renderer.
    renderer_id                 TEXT NOT NULL,

    -- Declared capability surface (mirrors ModelRendererCapabilityContract).
    platform                    TEXT NOT NULL,
    supported_component_kinds   TEXT[] NOT NULL DEFAULT '{}',
    interaction_model           TEXT NOT NULL,
    accessibility_tier          TEXT NOT NULL,
    contract_version            TEXT NOT NULL,

    -- Heartbeat freshness.
    declared_at                 TIMESTAMPTZ NOT NULL,
    last_heartbeat              TIMESTAMPTZ NOT NULL,
    is_degraded                 BOOLEAN NOT NULL DEFAULT FALSE,
    empty_state_reason          TEXT,                    -- typed EnumEmptyStateReason value; 'upstream-blocked' when degraded

    -- Projection lineage.
    observed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_renderer_capability_renderer_id UNIQUE (renderer_id)
);

-- ---- BEGIN OMN-15376 shape reconciliation: renderer_capability_projection ----
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

ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS renderer_id TEXT;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS platform TEXT;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS supported_component_kinds TEXT[] DEFAULT '{}';
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS interaction_model TEXT;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS accessibility_tier TEXT;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS contract_version TEXT;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS declared_at TIMESTAMPTZ;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS last_heartbeat TIMESTAMPTZ;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS is_degraded BOOLEAN DEFAULT FALSE;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS empty_state_reason TEXT;
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE renderer_capability_projection ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'renderer_id', 'platform', 'supported_component_kinds', 'interaction_model', 'accessibility_tier', 'contract_version', 'declared_at', 'last_heartbeat', 'is_degraded', 'observed_at', 'updated_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'renderer_capability_projection'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'renderer_capability_projection'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge renderer_capability_projection.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'renderer_capability_projection'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE renderer_capability_projection ADD CONSTRAINT renderer_capability_projection_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'renderer_capability_projection'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['renderer_id']::text[]
    ) THEN
        ALTER TABLE renderer_capability_projection ADD CONSTRAINT uq_renderer_capability_renderer_id UNIQUE (renderer_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: renderer_capability_projection ----


CREATE INDEX IF NOT EXISTS ix_renderer_capability_last_heartbeat
    ON renderer_capability_projection (last_heartbeat DESC);
