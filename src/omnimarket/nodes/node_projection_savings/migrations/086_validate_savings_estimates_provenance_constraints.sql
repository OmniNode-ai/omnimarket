-- OMN-15533: validate the provenance-vocabulary constraints after adding them as
-- NOT VALID.
--
-- Migration 085 adds the CHECK constraints without scanning the table. The
-- projection migration runner executes each migration file as one SQL batch, so
-- validation lives in this follow-up migration to keep the validation scan out of
-- the same transaction as the constraint-definition change -- the same split
-- migration 084 makes for the token-count constraints added in 082.

ALTER TABLE public.savings_estimates
    VALIDATE CONSTRAINT savings_estimates_savings_method_check;

ALTER TABLE public.savings_estimates
    VALIDATE CONSTRAINT savings_estimates_usage_source_check;
