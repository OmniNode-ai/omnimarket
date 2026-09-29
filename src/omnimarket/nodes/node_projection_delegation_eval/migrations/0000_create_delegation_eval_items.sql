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
ALTER TABLE public.delegation_eval_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.delegation_eval_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_items;
CREATE POLICY tenant_isolation ON public.delegation_eval_items
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_eval_items TO app_dashboard;
