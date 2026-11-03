-- OMN-19991: one row per local alert, per tenant, code and subject, folded from
-- onex.evt.omnimarket.local-alert-transitioned.v1.
-- Owner: omnimarket.nodes.node_projection_local_alerts
-- Target database: omnidash_analytics; physical schema: public.
--
-- The key is (tenant_id, code, subject), the subject being <mode>/<service>, so a
-- repeated condition updates one row. A row is a function of the newest
-- transition for its key, ordered by (last_transition_at, last_transition_id): a
-- replayed or older transition writes nothing. An alert clears only from a later
-- transition; nothing else changes state. The cursor is re-stamped from the
-- sequence on every write.

CREATE TABLE IF NOT EXISTS public.local_alerts (
    tenant_id TEXT NOT NULL,
    code TEXT NOT NULL CHECK (code <> ''),
    subject TEXT NOT NULL CHECK (subject <> ''),
    mode TEXT NOT NULL CHECK (mode IN ('native', 'compose')),
    service TEXT NOT NULL CHECK (service <> ''),
    state TEXT NOT NULL CHECK (state IN ('raised', 'cleared')),
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'warning')),
    raised_at TIMESTAMPTZ NOT NULL,
    cleared_at TIMESTAMPTZ,
    evidence JSONB NOT NULL,
    last_transition_id UUID NOT NULL,
    last_transition_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL NOT NULL,
    PRIMARY KEY (tenant_id, code, subject),
    -- Named here because they span two columns: a cleared alert carries its
    -- clear time and a raised one never does, and an alert never clears before
    -- it was raised.
    CONSTRAINT local_alerts_cleared_complete_check CHECK (
        (state = 'cleared') = (cleared_at IS NOT NULL)
    ),
    CONSTRAINT local_alerts_cleared_after_raised_check CHECK (
        cleared_at IS NULL OR cleared_at >= raised_at
    )
);

-- ---- BEGIN OMN-15376 shape reconciliation: local_alerts ----
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS code TEXT;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS subject TEXT;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS service TEXT;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS severity TEXT;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS raised_at TIMESTAMPTZ;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS cleared_at TIMESTAMPTZ;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS evidence JSONB;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS last_transition_id UUID;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS last_transition_at TIMESTAMPTZ;
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.local_alerts ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

-- Static NOT NULL convergence: a column already holding NULLs fails the ALTER, the
-- same outcome as a guard would give, with every relation target visible to the SQL gate.
ALTER TABLE public.local_alerts ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN code SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN subject SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN mode SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN service SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN state SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN severity SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN raised_at SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN evidence SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN last_transition_id SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN last_transition_at SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN updated_at SET NOT NULL;
ALTER TABLE public.local_alerts ALTER COLUMN projection_cursor SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_pkey PRIMARY KEY (tenant_id, code, subject);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_code_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_code_check CHECK (code <> '');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_subject_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_subject_check CHECK (subject <> '');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_mode_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_mode_check CHECK (mode IN ('native', 'compose'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_service_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_service_check CHECK (service <> '');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_state_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_state_check CHECK (state IN ('raised', 'cleared'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_severity_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_severity_check CHECK (severity IN ('critical', 'warning'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_cleared_complete_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_cleared_complete_check CHECK ((state = 'cleared') = (cleared_at IS NOT NULL));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_alerts'::regclass
          AND contype = 'c' AND conname = 'local_alerts_cleared_after_raised_check'
    ) THEN
        ALTER TABLE public.local_alerts
            ADD CONSTRAINT local_alerts_cleared_after_raised_check CHECK (cleared_at IS NULL OR cleared_at >= raised_at);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: local_alerts ----

CREATE UNIQUE INDEX IF NOT EXISTS idx_local_alerts_projection_cursor
    ON public.local_alerts (projection_cursor);

-- Tenant isolation from the first migration: the table is created with row-level
-- security enabled and its policy in place. FORCE is a separate, fenced step
-- (0002), so this file and the 0001 grants apply on every lane.
ALTER TABLE public.local_alerts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.local_alerts;
CREATE POLICY tenant_isolation ON public.local_alerts
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT SELECT ON public.local_alerts TO app_dashboard;
