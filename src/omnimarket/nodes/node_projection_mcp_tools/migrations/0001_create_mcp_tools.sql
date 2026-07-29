-- OMN-13080: Create the mcp_tools snapshot projection table.
-- DDL owner: omnimarket.nodes.node_projection_mcp_tools.
-- Consumed by: omnidash mcp-tools widget via onex.snapshot.projection.mcp-tools.v1.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS mcp_tools (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tool_name TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  correlation_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  is_active BOOLEAN NOT NULL DEFAULT true,
  mcp_tags TEXT[] NOT NULL DEFAULT '{}',
  metadata JSONB NOT NULL DEFAULT '{}',
  registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: mcp_tools ----
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

ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS tool_name TEXT;
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS description TEXT DEFAULT '';
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT '';
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS correlation_id TEXT DEFAULT '';
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active';
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true;
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS mcp_tags TEXT[] DEFAULT '{}';
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'tool_name', 'description', 'model_id', 'correlation_id', 'status', 'is_active', 'mcp_tags', 'metadata', 'registered_at', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'mcp_tools'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'mcp_tools'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge mcp_tools.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'mcp_tools'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE mcp_tools ADD CONSTRAINT mcp_tools_pkey PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'mcp_tools'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['tool_name']::text[]
    ) THEN
        ALTER TABLE mcp_tools ADD CONSTRAINT mcp_tools_tool_name_key UNIQUE (tool_name);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: mcp_tools ----


CREATE INDEX IF NOT EXISTS idx_mcp_tools_status
  ON mcp_tools (status);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_registered_at
  ON mcp_tools (registered_at DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_is_active
  ON mcp_tools (is_active);
