-- OMN-15533: persist the task class and served token counts on savings_estimates.
--
-- WHY THIS EXISTS
-- ---------------
-- projection_delegation_savings (migration 076) aliased `model_local AS task_type`
-- and hardcoded `0::int AS prompt_tokens` / `0::int AS completion_tokens` on the
-- savings_estimates branch. That was not a typo: savings_estimates never carried
-- a task_type or a token count, so the view had nothing else to select. The
-- 13-class delegation-matrix rerun on the .201 dev lane (2026-07-30) therefore
-- rendered `task_type = "gemini-2.5-flash"` on a row whose real task class was
-- `escalation`, and 0/0 tokens on rows whose source terminals carried 122/1248
-- and 109/6802.
--
-- The data was never missing upstream — it was discarded at the projection seam:
--   * ModelTaskDelegatedSavingsSource.task_type is a REQUIRED field (min_length=1)
--   * from_canonical_payload() resolves cumulative_input_tokens /
--     cumulative_output_tokens to build the premium counterfactual
--   * ...and ModelDelegateSkillSavingsProjection then carried none of the three
--     onto the savings_estimates row.
--
-- These columns close that seam. Migration 083 repoints the read views onto them.
--
-- NULLABLE, NO DEFAULT — DELIBERATE
-- ---------------------------------
-- A row written before this migration genuinely has no recorded task class and no
-- recorded token count. Defaulting to '' or to 0 would manufacture a value that
-- was never observed, which is the exact defect this ticket removes (and the
-- reason `0::int AS prompt_tokens` was wrong in the first place). NULL here means
-- "not recorded", and migration 083's provenance labelling reads it that way: a
-- row with NULL tokens is labelled `estimated` / `unknown`, never `measured`.
--
-- Backfill from delegation_events is NOT attempted: that table holds 0 rows on
-- the dev lane (node_projection_delegation has no live consumer group — OMN-14153)
-- and is explicitly out of scope on OMN-15533.

ALTER TABLE public.savings_estimates ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE public.savings_estimates ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER;
ALTER TABLE public.savings_estimates ADD COLUMN IF NOT EXISTS completion_tokens INTEGER;

-- Token counts are physical quantities: negative is always a write-path bug, and
-- NULL ("not recorded") stays permitted.
ALTER TABLE public.savings_estimates
    DROP CONSTRAINT IF EXISTS savings_estimates_prompt_tokens_check;
ALTER TABLE public.savings_estimates
    ADD CONSTRAINT savings_estimates_prompt_tokens_check
    CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0);

ALTER TABLE public.savings_estimates
    DROP CONSTRAINT IF EXISTS savings_estimates_completion_tokens_check;
ALTER TABLE public.savings_estimates
    ADD CONSTRAINT savings_estimates_completion_tokens_check
    CHECK (completion_tokens IS NULL OR completion_tokens >= 0);

-- task_type is a class label, never a model identifier. An empty string is the
-- writer's "no class supplied" signal and stays distinct from NULL ("column
-- predates the row"); neither is allowed to be a model name by construction,
-- because the writer now sources task_type from the terminal's own task_type
-- field rather than from model_local.
COMMENT ON COLUMN public.savings_estimates.task_type IS
    'OMN-15533: task class from the source delegation terminal (e.g. escalation, '
    'document). NULL = not recorded (row predates this column). Never a model name.';
COMMENT ON COLUMN public.savings_estimates.prompt_tokens IS
    'OMN-15533: served input tokens from the source terminal. NULL = not recorded.';
COMMENT ON COLUMN public.savings_estimates.completion_tokens IS
    'OMN-15533: served output tokens from the source terminal. NULL = not recorded.';
