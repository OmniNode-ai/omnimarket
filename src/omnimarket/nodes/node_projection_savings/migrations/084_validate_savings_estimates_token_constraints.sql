-- OMN-15533: validate token-count constraints after adding them as NOT VALID.
--
-- Migration 082 adds the CHECK constraints without scanning the table. The
-- projection migration runner executes each migration file as one SQL batch, so
-- validation lives in this follow-up migration to keep the validation scan out
-- of the same transaction as the constraint-definition change.

ALTER TABLE public.savings_estimates
    VALIDATE CONSTRAINT savings_estimates_prompt_tokens_check;

ALTER TABLE public.savings_estimates
    VALIDATE CONSTRAINT savings_estimates_completion_tokens_check;
