-- OMN-19793 (EV.4). Owned by omnimarket.nodes.node_projection_delegation_eval.
-- delegation_eval_results: one row per (tenant, eval run, task class, stratum, arm): false passes first, then false refusals, each with its decision bound, Wilson interval and range-line verdict. Content-free. The routing read (EV.9) reads the latest run per class.
CREATE TABLE IF NOT EXISTS public.delegation_eval_results (
    tenant_id UUID NOT NULL,
    eval_run_id UUID NOT NULL,
    task_class TEXT NOT NULL,
    stratum TEXT NOT NULL,
    arm TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    label_set_sha256 TEXT NOT NULL,
    gate_version TEXT NOT NULL,
    rater_role TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    total_n INT,
    accepted_n INT,
    false_pass_count INT,
    false_pass_rate DOUBLE PRECISION,
    false_pass_upper_bound DOUBLE PRECISION,
    false_pass_wilson_low DOUBLE PRECISION,
    false_pass_wilson_high DOUBLE PRECISION,
    false_pass_line_verdict TEXT,
    false_pass_required_n INT,
    refused_n INT,
    false_refusal_count INT,
    false_refusal_rate DOUBLE PRECISION,
    false_refusal_upper_bound DOUBLE PRECISION,
    false_refusal_wilson_low DOUBLE PRECISION,
    false_refusal_wilson_high DOUBLE PRECISION,
    false_refusal_line_verdict TEXT,
    undetermined_n INT,
    undetermined_share DOUBLE PRECISION,
    observed_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, eval_run_id, task_class, stratum, arm)
);
-- ---- BEGIN OMN-15376 shape reconciliation: delegation_eval_results ----
-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so CREATE TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS eval_run_id UUID;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS task_class TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS stratum TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS arm TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS manifest_id TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS label_set_sha256 TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS gate_version TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS rater_role TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS rubric_version TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS total_n INT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS accepted_n INT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_pass_count INT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_pass_rate DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_pass_upper_bound DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_pass_wilson_low DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_pass_wilson_high DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_pass_line_verdict TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_pass_required_n INT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS refused_n INT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_refusal_count INT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_refusal_rate DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_refusal_upper_bound DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_refusal_wilson_low DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_refusal_wilson_high DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS false_refusal_line_verdict TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS undetermined_n INT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS undetermined_share DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
-- DEFAULT RECONCILIATION: a pre-existing table keeps the columns it has, so the
-- declared defaults are set explicitly (idempotent).
ALTER TABLE public.delegation_eval_results
    ALTER COLUMN first_seen_at SET DEFAULT NOW();
ALTER TABLE public.delegation_eval_results
    ALTER COLUMN updated_at SET DEFAULT NOW();
-- NOT NULL RECONCILIATION: SET NOT NULL refuses (fails the migration) when a
-- pre-existing row holds NULL; it never invents a value.
ALTER TABLE public.delegation_eval_results ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN eval_run_id SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN task_class SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN stratum SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN arm SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN manifest_id SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN label_set_sha256 SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN gate_version SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN rater_role SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN rubric_version SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN first_seen_at SET NOT NULL;
ALTER TABLE public.delegation_eval_results ALTER COLUMN updated_at SET NOT NULL;
-- PRIMARY KEY RECONCILIATION.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_eval_results'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.delegation_eval_results
            ADD CONSTRAINT delegation_eval_results_pkey
            PRIMARY KEY (tenant_id, eval_run_id, task_class, stratum, arm);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: delegation_eval_results ----
-- The routing read (EV.9) asks for the latest run per class.
CREATE INDEX IF NOT EXISTS delegation_eval_results_class_latest_idx
    ON public.delegation_eval_results (tenant_id, task_class, stratum, arm, observed_at DESC);
ALTER TABLE public.delegation_eval_results ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_results;
CREATE POLICY tenant_isolation ON public.delegation_eval_results
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_eval_results TO app_dashboard;
