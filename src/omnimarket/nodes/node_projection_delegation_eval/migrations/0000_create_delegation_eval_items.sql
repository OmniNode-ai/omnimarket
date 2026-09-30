-- OMN-19790. Owned by omnimarket.nodes.node_projection_delegation_eval.
-- Prompt/response content lives on the lab table only: omnimarket is public.
CREATE TABLE IF NOT EXISTS public.delegation_eval_items (
    tenant_id UUID NOT NULL,
    item_key TEXT NOT NULL,
    correlation_id TEXT,
    attempt_index INT,
    task_class TEXT,
    stratum TEXT,
    prompt_snapshot TEXT,
    response_snapshot TEXT,
    gate_verdict TEXT,
    deciding_check TEXT,
    label TEXT,
    rater_role TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    computed_facts JSONB,
    observed_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, item_key, rater_role, rubric_version)
);
-- ---- BEGIN OMN-15376 shape reconciliation: delegation_eval_items ----
-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so CREATE TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS item_key TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS attempt_index INT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS task_class TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS stratum TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS prompt_snapshot TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS response_snapshot TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS gate_verdict TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS deciding_check TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS rater_role TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS rubric_version TEXT;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS computed_facts JSONB;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_items
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
-- DEFAULT RECONCILIATION: a pre-existing table keeps the columns it has, so the
-- declared defaults are set explicitly (idempotent).
ALTER TABLE public.delegation_eval_items
    ALTER COLUMN first_seen_at SET DEFAULT NOW();
ALTER TABLE public.delegation_eval_items
    ALTER COLUMN updated_at SET DEFAULT NOW();
-- NOT NULL RECONCILIATION: SET NOT NULL refuses (fails the migration) when a
-- pre-existing row holds NULL; it never invents a value.
ALTER TABLE public.delegation_eval_items ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.delegation_eval_items ALTER COLUMN item_key SET NOT NULL;
ALTER TABLE public.delegation_eval_items ALTER COLUMN rater_role SET NOT NULL;
ALTER TABLE public.delegation_eval_items ALTER COLUMN rubric_version SET NOT NULL;
ALTER TABLE public.delegation_eval_items ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE public.delegation_eval_items ALTER COLUMN first_seen_at SET NOT NULL;
ALTER TABLE public.delegation_eval_items ALTER COLUMN updated_at SET NOT NULL;
-- PRIMARY KEY RECONCILIATION.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_eval_items'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.delegation_eval_items
            ADD CONSTRAINT delegation_eval_items_pkey
            PRIMARY KEY (tenant_id, item_key, rater_role, rubric_version);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: delegation_eval_items ----
ALTER TABLE public.delegation_eval_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_items;
CREATE POLICY tenant_isolation ON public.delegation_eval_items
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_eval_items TO app_dashboard;
