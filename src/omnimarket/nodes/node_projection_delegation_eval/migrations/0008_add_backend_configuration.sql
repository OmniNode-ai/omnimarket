-- Eval-only provenance. Old rows retain NULL and do not gain invented identity.
ALTER TABLE public.delegation_eval_item_verdicts
    ADD COLUMN IF NOT EXISTS backend_configuration JSONB,
    ADD COLUMN IF NOT EXISTS latency_ms DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS backend_configuration JSONB;
