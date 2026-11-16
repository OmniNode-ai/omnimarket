-- OMN-24615 AC1: the runs of a window that ended with a failure cause, served
-- beside runs_unknown_tokens and runs_unknown_spend.
--
-- A run whose answer was refused, timed out, errored at the provider or went
-- back to its caller saved nothing: the fold books it measured with zero
-- savings and keeps its spend, and this column names how many there were.
-- Nullable with no default, so existing rows read NULL, never zero, until the
-- next refresh replaces them.

ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS runs_failed INTEGER;
