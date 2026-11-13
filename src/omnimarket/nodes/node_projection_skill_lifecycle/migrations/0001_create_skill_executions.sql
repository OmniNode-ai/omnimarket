-- Migration: 0001_create_skill_executions.sql
-- Purpose: the skill_executions table written by node_projection_skill_lifecycle.
--
-- The table is the one omnibase_infra's forward migration 048 creates for the
-- skill lifecycle consumer this node replaces; this file repeats that
-- definition with guarded reconciliation for an older, incomplete table.
-- Missing required values are refused rather than fabricated.
--
--   * One row per skill-started or skill-completed event.
--   * event_id is the primary key and the UPSERT conflict target.
--   * run_id joins a started row to its completed row.
--   * Columns present on only one event type are nullable.

CREATE TABLE IF NOT EXISTS public.skill_executions (
    event_id                UUID            PRIMARY KEY,
    run_id                  UUID            NOT NULL,
    event_type              TEXT            NOT NULL CHECK (event_type IN ('started', 'completed')),
    skill_name              TEXT            NOT NULL,
    skill_id                TEXT,
    repo_id                 TEXT            NOT NULL,
    correlation_id          UUID            NOT NULL,
    args_count              INTEGER,
    status                  TEXT CHECK (status IN ('success', 'failed', 'partial')),
    duration_ms             INTEGER,
    error_type              TEXT,
    started_emit_failed     BOOLEAN         DEFAULT FALSE,
    session_id              TEXT,
    emitted_at              TIMESTAMPTZ     NOT NULL,
    received_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- ---- BEGIN OMN-15376 shape reconciliation: skill_executions ----
-- Add missing columns before creating indexes. Required NULLs in historical
-- rows fail at SET NOT NULL; this migration never invents event data.
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS event_id UUID;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS run_id UUID;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS skill_name TEXT;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS skill_id TEXT;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS repo_id TEXT;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS correlation_id UUID;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS args_count INTEGER;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS duration_ms INTEGER;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS error_type TEXT;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS started_emit_failed BOOLEAN         DEFAULT FALSE;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS emitted_at TIMESTAMPTZ;
ALTER TABLE public.skill_executions ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ      DEFAULT NOW();

ALTER TABLE public.skill_executions ALTER COLUMN event_id SET NOT NULL;
ALTER TABLE public.skill_executions ALTER COLUMN run_id SET NOT NULL;
ALTER TABLE public.skill_executions ALTER COLUMN event_type SET NOT NULL;
ALTER TABLE public.skill_executions ALTER COLUMN skill_name SET NOT NULL;
ALTER TABLE public.skill_executions ALTER COLUMN repo_id SET NOT NULL;
ALTER TABLE public.skill_executions ALTER COLUMN correlation_id SET NOT NULL;
ALTER TABLE public.skill_executions ALTER COLUMN emitted_at SET NOT NULL;
ALTER TABLE public.skill_executions ALTER COLUMN received_at SET NOT NULL;

DO $reconcile_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.skill_executions'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.skill_executions
            ADD CONSTRAINT skill_executions_pkey PRIMARY KEY (event_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.skill_executions'::regclass
          AND conname = 'skill_executions_event_type_check'
    ) THEN
        ALTER TABLE public.skill_executions
            ADD CONSTRAINT skill_executions_event_type_check
            CHECK (event_type IN ('started', 'completed'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.skill_executions'::regclass
          AND conname = 'skill_executions_status_check'
    ) THEN
        ALTER TABLE public.skill_executions
            ADD CONSTRAINT skill_executions_status_check
            CHECK (status IN ('success', 'failed', 'partial'));
    END IF;
END
$reconcile_constraints$;
-- ---- END OMN-15376 shape reconciliation: skill_executions ----

CREATE INDEX IF NOT EXISTS idx_skill_executions_run_id
    ON public.skill_executions (run_id);
CREATE INDEX IF NOT EXISTS idx_skill_executions_skill_name
    ON public.skill_executions (skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_executions_emitted_at
    ON public.skill_executions (emitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_skill_executions_repo_id
    ON public.skill_executions (repo_id);
CREATE INDEX IF NOT EXISTS idx_skill_executions_skill_event_type
    ON public.skill_executions (skill_name, event_type);
