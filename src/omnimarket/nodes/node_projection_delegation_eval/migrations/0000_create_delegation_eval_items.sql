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
ALTER TABLE public.delegation_eval_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.delegation_eval_items FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_items;
CREATE POLICY tenant_isolation ON public.delegation_eval_items
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_eval_items TO app_dashboard;
