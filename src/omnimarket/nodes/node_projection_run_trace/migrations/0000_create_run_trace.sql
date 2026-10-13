-- OMN-19987: one row per step of one delegation run, per tenant.
-- Owner: omnimarket.nodes.node_projection_run_trace
-- Target database: omnidash_analytics; physical schema: public.
--
-- The key is (tenant_id, correlation_id, step_sequence). step_sequence is the
-- number the delegate orchestrator gave the step; it is the run's only order.
-- started_at is shown, never sorted on: two steps may share one timestamp.

CREATE TABLE IF NOT EXISTS public.run_trace (
    tenant_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    step_sequence INTEGER NOT NULL CHECK (step_sequence >= 1),
    step_kind TEXT NOT NULL CHECK (step_kind IN ('request', 'routing_decision', 'provider_call', 'quality_gate', 'terminal')),
    started_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT CHECK (duration_ms IS NULL OR duration_ms >= 0),
    outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
    failure_cause TEXT,
    backend_id TEXT,
    model_id TEXT,
    attempt_number INTEGER CHECK (attempt_number IS NULL OR attempt_number >= 1),
    reason TEXT,
    candidates JSONB NOT NULL DEFAULT '[]'::jsonb,
    failing_step_sequence INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL NOT NULL,
    PRIMARY KEY (tenant_id, correlation_id, step_sequence),
    -- Named here because it spans two columns: Postgres would otherwise call it
    -- run_trace_check, and the reconciliation below would add it a second time.
    CONSTRAINT run_trace_failing_step_sequence_check CHECK (
        failing_step_sequence IS NULL
        OR (failing_step_sequence >= 1 AND failing_step_sequence < step_sequence)
    )
);

-- ---- BEGIN OMN-15376 shape reconciliation: run_trace ----
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS step_sequence INTEGER;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS step_kind TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS duration_ms BIGINT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS failure_cause TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS backend_id TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS attempt_number INTEGER;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS candidates JSONB DEFAULT '[]'::jsonb;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS failing_step_sequence INTEGER;
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.run_trace ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

-- Static NOT NULL convergence: a column already holding NULLs fails the ALTER, the
-- same outcome as a guard would give, with every relation target visible to the SQL gate.
ALTER TABLE public.run_trace ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN correlation_id SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN step_sequence SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN step_kind SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN started_at SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN outcome SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN candidates SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN updated_at SET NOT NULL;
ALTER TABLE public.run_trace ALTER COLUMN projection_cursor SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.run_trace'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.run_trace
            ADD CONSTRAINT run_trace_pkey PRIMARY KEY (tenant_id, correlation_id, step_sequence);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.run_trace'::regclass
          AND contype = 'c' AND conname = 'run_trace_step_sequence_check'
    ) THEN
        ALTER TABLE public.run_trace
            ADD CONSTRAINT run_trace_step_sequence_check CHECK (step_sequence >= 1);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.run_trace'::regclass
          AND contype = 'c' AND conname = 'run_trace_step_kind_check'
    ) THEN
        ALTER TABLE public.run_trace
            ADD CONSTRAINT run_trace_step_kind_check CHECK (step_kind IN ('request', 'routing_decision', 'provider_call', 'quality_gate', 'terminal'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.run_trace'::regclass
          AND contype = 'c' AND conname = 'run_trace_outcome_check'
    ) THEN
        ALTER TABLE public.run_trace
            ADD CONSTRAINT run_trace_outcome_check CHECK (outcome IN ('succeeded', 'failed'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.run_trace'::regclass
          AND contype = 'c' AND conname = 'run_trace_duration_ms_check'
    ) THEN
        ALTER TABLE public.run_trace
            ADD CONSTRAINT run_trace_duration_ms_check CHECK (duration_ms IS NULL OR duration_ms >= 0);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.run_trace'::regclass
          AND contype = 'c' AND conname = 'run_trace_attempt_number_check'
    ) THEN
        ALTER TABLE public.run_trace
            ADD CONSTRAINT run_trace_attempt_number_check CHECK (attempt_number IS NULL OR attempt_number >= 1);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.run_trace'::regclass
          AND contype = 'c' AND conname = 'run_trace_failing_step_sequence_check'
    ) THEN
        ALTER TABLE public.run_trace
            ADD CONSTRAINT run_trace_failing_step_sequence_check CHECK (failing_step_sequence IS NULL OR (failing_step_sequence >= 1 AND failing_step_sequence < step_sequence));
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: run_trace ----

CREATE UNIQUE INDEX IF NOT EXISTS idx_run_trace_projection_cursor
    ON public.run_trace (projection_cursor);
CREATE INDEX IF NOT EXISTS idx_run_trace_run
    ON public.run_trace (correlation_id, step_sequence);

-- Tenant isolation from the first migration: the table is created with row-level
-- security enabled and its policy in place. FORCE is a separate, fenced step
-- (0002), so this file and the 0001 grants apply on every lane.
ALTER TABLE public.run_trace ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.run_trace;
CREATE POLICY tenant_isolation ON public.run_trace
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT SELECT ON public.run_trace TO app_dashboard;
