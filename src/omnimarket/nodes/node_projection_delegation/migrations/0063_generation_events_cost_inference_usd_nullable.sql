-- OMN-20834: generation_events.cost_inference_usd may be NULL, and NULL means
-- "this run was not priced", never zero.
--
-- 0008 declared cost_inference_usd NUMERIC(18, 6) NOT NULL DEFAULT 0, so a run
-- on a model the pricing surface reports as UNKNOWN was stored as 0 --
-- indistinguishable from a run that was priced and really cost nothing (a free
-- local model). The generation consumer now emits null for such a run and the
-- projection writes it through.
--
-- Drop the NOT NULL and the DEFAULT. Existing rows are left as they are: an old
-- 0 cannot be told apart from a free run, so it is not rewritten.

BEGIN;

ALTER TABLE generation_events
    ALTER COLUMN cost_inference_usd DROP NOT NULL,
    ALTER COLUMN cost_inference_usd DROP DEFAULT;

COMMIT;
