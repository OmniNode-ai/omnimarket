-- Harness trials share the existing eval rows; run ids include their identity.
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS harness TEXT,
    ADD COLUMN IF NOT EXISTS model TEXT,
    ADD COLUMN IF NOT EXISTS configuration TEXT,
    ADD COLUMN IF NOT EXISTS duration_s DOUBLE PRECISION CHECK (duration_s >= 0),
    ADD COLUMN IF NOT EXISTS failure_kind TEXT;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS harness TEXT,
    ADD COLUMN IF NOT EXISTS model TEXT,
    ADD COLUMN IF NOT EXISTS configuration TEXT;
CREATE INDEX IF NOT EXISTS delegation_eval_results_harness_class_idx
    ON public.delegation_eval_results (tenant_id, harness, model, configuration, task_class)
    WHERE harness IS NOT NULL;
