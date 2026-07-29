-- OMN-12942: base table for the routing-decision dashboard projection view.
--
-- node_projection_llm_routing/0001_create_routing_dashboard_projection_view.sql
-- composes the `projection_routing_decision` view over `llm_routing_decisions`.
-- That base table is also created by the flat infra forward set
-- (omnibase_infra docker/migrations/forward/065_create_llm_routing_decisions.sql),
-- which the runner applies in its first phase. But this node-owned migration set
-- must be SELF-CONTAINED: the node forward-migration runner applies node dirs in
-- their own pass, and a clean apply of the node set alone (the path used by the
-- node-migration vendoring/sync flow) hard-fails the view migration when the flat
-- infra table is absent — exactly what happened during the 2026-06-11 04:24Z
-- stability hot-patch, which forced the view migration to be pruned. Owning the
-- base table inside the node removes that ordering dependency.
--
-- Schema is identical to the flat infra 065 table. All statements are
-- IF NOT EXISTS, so re-applying after 065 (or vice-versa) is a no-op — the two
-- migrations converge on the same table and are safe in either order.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS llm_routing_decisions (
    id                      UUID        NOT NULL DEFAULT gen_random_uuid(),
    correlation_id          UUID        NOT NULL,
    session_id              TEXT,

    -- Routing decision
    llm_agent               TEXT        NOT NULL,
    fuzzy_agent             TEXT,
    agreement               BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Confidence scores (0-1, NULL when not provided)
    llm_confidence          NUMERIC(5, 4) CHECK (llm_confidence IS NULL OR (llm_confidence >= 0 AND llm_confidence <= 1)),
    fuzzy_confidence        NUMERIC(5, 4) CHECK (fuzzy_confidence IS NULL OR (fuzzy_confidence >= 0 AND fuzzy_confidence <= 1)),

    -- Latency
    llm_latency_ms          INTEGER     NOT NULL DEFAULT 0,
    fuzzy_latency_ms        INTEGER     NOT NULL DEFAULT 0,

    -- Routing metadata
    used_fallback           BOOLEAN     NOT NULL DEFAULT FALSE,
    routing_prompt_version  TEXT        NOT NULL DEFAULT 'unknown',
    intent                  TEXT,
    model                   TEXT,
    cost_usd                NUMERIC(12, 8) CHECK (cost_usd IS NULL OR cost_usd >= 0),

    -- Audit
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projected_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_llm_routing_decisions PRIMARY KEY (id),
    CONSTRAINT uq_llm_routing_decisions_correlation UNIQUE (correlation_id)
);

-- ---- BEGIN OMN-15376 shape reconciliation: llm_routing_decisions ----
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

ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS correlation_id UUID;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS llm_agent TEXT;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS fuzzy_agent TEXT;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS agreement BOOLEAN DEFAULT FALSE;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS llm_confidence NUMERIC(5, 4);
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS fuzzy_confidence NUMERIC(5, 4);
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS llm_latency_ms INTEGER DEFAULT 0;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS fuzzy_latency_ms INTEGER DEFAULT 0;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS used_fallback BOOLEAN DEFAULT FALSE;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS routing_prompt_version TEXT DEFAULT 'unknown';
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS intent TEXT;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12, 8);
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE llm_routing_decisions ADD COLUMN IF NOT EXISTS projected_at TIMESTAMPTZ DEFAULT NOW();

DO $$
DECLARE
    v_col  TEXT;
    v_nulls BIGINT;
BEGIN
    FOREACH v_col IN ARRAY ARRAY['id', 'correlation_id', 'llm_agent', 'agreement', 'llm_latency_ms', 'fuzzy_latency_ms', 'used_fallback', 'routing_prompt_version', 'created_at', 'projected_at']
    LOOP
        EXECUTE format(
            'SELECT count(*) FROM %s WHERE %I IS NULL', 'llm_routing_decisions'::regclass, v_col
        ) INTO v_nulls;
        IF v_nulls = 0 THEN
            EXECUTE format(
                'ALTER TABLE %s ALTER COLUMN %I SET NOT NULL', 'llm_routing_decisions'::regclass, v_col
            );
        ELSE
            RAISE EXCEPTION
                'OMN-15376: cannot converge llm_routing_decisions.% to NOT NULL -- % pre-existing row(s) hold NULL. This needs a data ruling (backfill value, or drop the NOT NULL from the contract); the migration refuses to guess.',
                v_col, v_nulls;
        END IF;
    END LOOP;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_routing_decisions'::regclass AND conname = 'llm_routing_decisions_llm_confidence_check'
    ) THEN
        ALTER TABLE llm_routing_decisions ADD CONSTRAINT llm_routing_decisions_llm_confidence_check CHECK (llm_confidence IS NULL OR (llm_confidence >= 0 AND llm_confidence <= 1));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_routing_decisions'::regclass AND conname = 'llm_routing_decisions_fuzzy_confidence_check'
    ) THEN
        ALTER TABLE llm_routing_decisions ADD CONSTRAINT llm_routing_decisions_fuzzy_confidence_check CHECK (fuzzy_confidence IS NULL OR (fuzzy_confidence >= 0 AND fuzzy_confidence <= 1));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_routing_decisions'::regclass AND conname = 'llm_routing_decisions_cost_usd_check'
    ) THEN
        ALTER TABLE llm_routing_decisions ADD CONSTRAINT llm_routing_decisions_cost_usd_check CHECK (cost_usd IS NULL OR cost_usd >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'llm_routing_decisions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE llm_routing_decisions ADD CONSTRAINT pk_llm_routing_decisions PRIMARY KEY (id);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'llm_routing_decisions'::regclass
          AND c.contype IN ('p', 'u')
          AND (
              SELECT array_agg(a.attname::text ORDER BY a.attname)
              FROM unnest(c.conkey) AS k(attnum)
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
          ) = ARRAY['correlation_id']::text[]
    ) THEN
        ALTER TABLE llm_routing_decisions ADD CONSTRAINT uq_llm_routing_decisions_correlation UNIQUE (correlation_id);
    END IF;
END$$;

-- ---- END OMN-15376 shape reconciliation: llm_routing_decisions ----


CREATE INDEX IF NOT EXISTS idx_lrd_created_at
    ON llm_routing_decisions (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_agreement
    ON llm_routing_decisions (agreement, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_used_fallback
    ON llm_routing_decisions (used_fallback, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_prompt_version
    ON llm_routing_decisions (routing_prompt_version, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lrd_agent_pair
    ON llm_routing_decisions (llm_agent, fuzzy_agent, created_at DESC)
    WHERE agreement = FALSE;

COMMENT ON TABLE llm_routing_decisions IS
    'LLM routing decision projection from onex.evt.omniclaude.llm-routing-decision.v1. '
    'UPSERT key: correlation_id (UUID). Base table for projection_routing_decision view (OMN-12942).';
