-- OMN-19988: the latest report of each local service, per tenant and mode, with
-- the verdict and open alert the local alert compute gave it.
-- Owner: omnimarket.nodes.node_projection_local_environment
-- Target database: omnidash_analytics; physical schema: public.
--
-- The key is (tenant_id, mode, service). A row changes in place on each newer
-- report, and a report of one service can re-judge its siblings, so the cursor
-- is re-stamped from the sequence on every write.

CREATE TABLE IF NOT EXISTS public.local_environment (
    tenant_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('native', 'compose')),
    service TEXT NOT NULL CHECK (service <> ''),
    observed_state TEXT NOT NULL CHECK (observed_state IN ('running', 'exited_ok', 'down', 'unhealthy')),
    reason TEXT NOT NULL,
    versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen TIMESTAMPTZ NOT NULL,
    last_change TIMESTAMPTZ NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('running', 'down', 'unhealthy', 'stale')),
    verdict_reason TEXT NOT NULL,
    alert_code TEXT,
    alert_severity TEXT CHECK (alert_severity IS NULL OR alert_severity IN ('critical', 'warning')),
    alert_since TIMESTAMPTZ,
    store_kind TEXT,
    store_location TEXT,
    access_mode TEXT,
    token_minted_at TIMESTAMPTZ,
    rotate_hint TEXT,
    report_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL NOT NULL,
    PRIMARY KEY (tenant_id, mode, service),
    -- Named here because it spans three columns: an alert is open only with its
    -- severity and its start, and never without both.
    CONSTRAINT local_environment_alert_complete_check CHECK (
        (alert_code IS NULL AND alert_severity IS NULL AND alert_since IS NULL)
        OR (alert_code IS NOT NULL AND alert_severity IS NOT NULL AND alert_since IS NOT NULL)
    )
);

-- ---- BEGIN OMN-15376 shape reconciliation: local_environment ----
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS service TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS observed_state TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS versions JSONB DEFAULT '{}'::jsonb;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS last_change TIMESTAMPTZ;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS verdict_reason TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS alert_code TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS alert_severity TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS alert_since TIMESTAMPTZ;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS store_kind TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS store_location TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS access_mode TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS token_minted_at TIMESTAMPTZ;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS rotate_hint TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS report_id TEXT;
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE public.local_environment ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

-- Static NOT NULL convergence: a column already holding NULLs fails the ALTER, the
-- same outcome as a guard would give, with every relation target visible to the SQL gate.
ALTER TABLE public.local_environment ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN mode SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN service SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN observed_state SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN reason SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN versions SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN last_seen SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN last_change SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN verdict SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN verdict_reason SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN report_id SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN updated_at SET NOT NULL;
ALTER TABLE public.local_environment ALTER COLUMN projection_cursor SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_environment'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.local_environment
            ADD CONSTRAINT local_environment_pkey PRIMARY KEY (tenant_id, mode, service);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_environment'::regclass
          AND contype = 'c' AND conname = 'local_environment_mode_check'
    ) THEN
        ALTER TABLE public.local_environment
            ADD CONSTRAINT local_environment_mode_check CHECK (mode IN ('native', 'compose'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_environment'::regclass
          AND contype = 'c' AND conname = 'local_environment_service_check'
    ) THEN
        ALTER TABLE public.local_environment
            ADD CONSTRAINT local_environment_service_check CHECK (service <> '');
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_environment'::regclass
          AND contype = 'c' AND conname = 'local_environment_observed_state_check'
    ) THEN
        ALTER TABLE public.local_environment
            ADD CONSTRAINT local_environment_observed_state_check CHECK (observed_state IN ('running', 'exited_ok', 'down', 'unhealthy'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_environment'::regclass
          AND contype = 'c' AND conname = 'local_environment_verdict_check'
    ) THEN
        ALTER TABLE public.local_environment
            ADD CONSTRAINT local_environment_verdict_check CHECK (verdict IN ('running', 'down', 'unhealthy', 'stale'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_environment'::regclass
          AND contype = 'c' AND conname = 'local_environment_alert_severity_check'
    ) THEN
        ALTER TABLE public.local_environment
            ADD CONSTRAINT local_environment_alert_severity_check CHECK (alert_severity IS NULL OR alert_severity IN ('critical', 'warning'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.local_environment'::regclass
          AND contype = 'c' AND conname = 'local_environment_alert_complete_check'
    ) THEN
        ALTER TABLE public.local_environment
            ADD CONSTRAINT local_environment_alert_complete_check CHECK ((alert_code IS NULL AND alert_severity IS NULL AND alert_since IS NULL) OR (alert_code IS NOT NULL AND alert_severity IS NOT NULL AND alert_since IS NOT NULL));
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: local_environment ----

CREATE UNIQUE INDEX IF NOT EXISTS idx_local_environment_projection_cursor
    ON public.local_environment (projection_cursor);

-- Tenant isolation from the first migration: the table is created with row-level
-- security enabled and its policy in place. FORCE is a separate, fenced step
-- (0002), so this file and the 0001 grants apply on every lane.
ALTER TABLE public.local_environment ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.local_environment;
CREATE POLICY tenant_isolation ON public.local_environment
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT SELECT ON public.local_environment TO app_dashboard;
