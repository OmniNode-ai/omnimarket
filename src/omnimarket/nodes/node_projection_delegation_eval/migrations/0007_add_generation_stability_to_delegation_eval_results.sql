-- OMN-19795 (EV.7). Owned by omnimarket.nodes.node_projection_delegation_eval.
-- delegation_eval_results gains each class's generation flip rate: the share of
-- repeated-generation groups whose three gated verdicts are not all the same,
-- its Wilson 95 percent interval, and the one backend it was measured on. All
-- four are NULL on a run that carried no repeated generations. Content-free.
-- Additive and idempotent: the existing grants and the tenant_isolation policy
-- of 0004-0006 cover the new columns.
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS flip_rate DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS flip_rate_wilson_low DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS flip_rate_wilson_high DOUBLE PRECISION;
ALTER TABLE public.delegation_eval_results
    ADD COLUMN IF NOT EXISTS flip_measured_backend_id TEXT;
