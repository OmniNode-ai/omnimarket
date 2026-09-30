-- OMN-19793 (EV.4). Owned by omnimarket.nodes.node_projection_delegation_eval.
-- delegation_eval_item_verdicts: one row per (tenant, eval run, labelled item): the recorded verdict beside the replayed verdict. Content-free.
CREATE TABLE IF NOT EXISTS public.delegation_eval_item_verdicts (
    tenant_id UUID NOT NULL,
    eval_run_id UUID NOT NULL,
    item_key TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    gate_version TEXT NOT NULL,
    rater_role TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    task_class TEXT,
    stratum TEXT,
    label TEXT,
    recorded_verdict TEXT,
    recorded_deciding_check TEXT,
    replayed_verdict TEXT,
    replayed_deciding_check TEXT,
    replay_count INT,
    observed_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor BIGSERIAL,
    PRIMARY KEY (tenant_id, eval_run_id, item_key)
);
-- ---- BEGIN OMN-15376 shape reconciliation: delegation_eval_item_verdicts ----
-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so CREATE TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS eval_run_id UUID;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS item_key TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS manifest_id TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS gate_version TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS rater_role TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS rubric_version TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS task_class TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS stratum TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS recorded_verdict TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS recorded_deciding_check TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS replayed_verdict TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS replayed_deciding_check TEXT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS replay_count INT;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;
-- DEFAULT RECONCILIATION: a pre-existing table keeps the columns it has, so the
-- declared defaults are set explicitly (idempotent).
ALTER TABLE public.delegation_eval_item_verdicts
    ALTER COLUMN first_seen_at SET DEFAULT NOW();
ALTER TABLE public.delegation_eval_item_verdicts
    ALTER COLUMN updated_at SET DEFAULT NOW();
-- NOT NULL RECONCILIATION: SET NOT NULL refuses (fails the migration) when a
-- pre-existing row holds NULL; it never invents a value.
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN eval_run_id SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN item_key SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN manifest_id SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN gate_version SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN rater_role SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN rubric_version SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN first_seen_at SET NOT NULL;
ALTER TABLE public.delegation_eval_item_verdicts ALTER COLUMN updated_at SET NOT NULL;
-- PRIMARY KEY RECONCILIATION.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.delegation_eval_item_verdicts'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.delegation_eval_item_verdicts
            ADD CONSTRAINT delegation_eval_item_verdicts_pkey
            PRIMARY KEY (tenant_id, eval_run_id, item_key);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: delegation_eval_item_verdicts ----
ALTER TABLE public.delegation_eval_item_verdicts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.delegation_eval_item_verdicts;
CREATE POLICY tenant_isolation ON public.delegation_eval_item_verdicts
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT ON public.delegation_eval_item_verdicts TO app_dashboard;
