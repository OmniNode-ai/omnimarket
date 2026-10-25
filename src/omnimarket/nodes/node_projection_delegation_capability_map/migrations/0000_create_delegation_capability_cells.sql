-- OMN-20169. Owned by omnimarket.nodes.node_projection_delegation_capability_map.
-- One capability cell per tenant, map run, model, backend, configuration, class and band.
-- Content free: verdict counts and bounds, never a prompt or an answer.
CREATE TABLE IF NOT EXISTS public.delegation_capability_cells (
    tenant_id UUID NOT NULL,
    map_run_id TEXT NOT NULL,
    model_key TEXT NOT NULL,
    backend_id TEXT NOT NULL,
    configuration_id TEXT NOT NULL,
    task_class TEXT NOT NULL,
    size_band TEXT NOT NULL CONSTRAINT delegation_capability_cells_size_band_check
        CHECK (size_band IN ('S', 'M', 'L')),
    configuration_parameters JSONB NOT NULL,
    n INT NOT NULL CONSTRAINT delegation_capability_cells_n_check CHECK (n >= 1),
    passes INT NOT NULL CONSTRAINT delegation_capability_cells_passes_check CHECK (passes >= 0 AND passes <= n),
    pass_rate DOUBLE PRECISION NOT NULL,
    wilson_low DOUBLE PRECISION NOT NULL,
    wilson_high DOUBLE PRECISION NOT NULL,
    decision_lower_bound DOUBLE PRECISION NOT NULL,
    floor DOUBLE PRECISION NOT NULL,
    required_n INT NOT NULL,
    verdict TEXT NOT NULL CONSTRAINT delegation_capability_cells_verdict_check
        CHECK (verdict IN ('MET', 'MISSED', 'REFUSED')),
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, map_run_id, model_key, backend_id, configuration_id,
                 task_class, size_band)
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_capability_cells ----
-- Guarded adds converge drifted tables without inventing required values.
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS map_run_id TEXT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS model_key TEXT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS backend_id TEXT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS configuration_id TEXT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS task_class TEXT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS size_band TEXT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS configuration_parameters JSONB;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS n INT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS passes INT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS pass_rate DOUBLE PRECISION;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS wilson_low DOUBLE PRECISION;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS wilson_high DOUBLE PRECISION;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS decision_lower_bound DOUBLE PRECISION;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS floor DOUBLE PRECISION;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS required_n INT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE public.delegation_capability_cells ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;

ALTER TABLE public.delegation_capability_cells ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN map_run_id SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN model_key SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN backend_id SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN configuration_id SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN task_class SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN size_band SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN configuration_parameters SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN n SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN passes SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN pass_rate SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN wilson_low SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN wilson_high SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN decision_lower_bound SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN floor SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN required_n SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN verdict SET NOT NULL;
ALTER TABLE public.delegation_capability_cells ALTER COLUMN observed_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_capability_cells'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.delegation_capability_cells
            ADD CONSTRAINT delegation_capability_cells_pkey
            PRIMARY KEY (tenant_id, map_run_id, model_key, backend_id, configuration_id,
                         task_class, size_band);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_capability_cells'::regclass
          AND contype = 'c' AND conname = 'delegation_capability_cells_size_band_check'
    ) THEN
        ALTER TABLE public.delegation_capability_cells
            ADD CONSTRAINT delegation_capability_cells_size_band_check CHECK (size_band IN ('S', 'M', 'L'));
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_capability_cells'::regclass
          AND contype = 'c' AND conname = 'delegation_capability_cells_n_check'
    ) THEN
        ALTER TABLE public.delegation_capability_cells
            ADD CONSTRAINT delegation_capability_cells_n_check CHECK (n >= 1);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_capability_cells'::regclass
          AND contype = 'c' AND conname = 'delegation_capability_cells_passes_check'
    ) THEN
        ALTER TABLE public.delegation_capability_cells
            ADD CONSTRAINT delegation_capability_cells_passes_check CHECK (passes >= 0 AND passes <= n);
    END IF;
END$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_capability_cells'::regclass
          AND contype = 'c' AND conname = 'delegation_capability_cells_verdict_check'
    ) THEN
        ALTER TABLE public.delegation_capability_cells
            ADD CONSTRAINT delegation_capability_cells_verdict_check CHECK (verdict IN ('MET', 'MISSED', 'REFUSED'));
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: delegation_capability_cells ----

ALTER TABLE public.delegation_capability_cells ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_capability_cells;
CREATE POLICY tenant_isolation ON public.delegation_capability_cells
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_capability_cells TO app_dashboard;
