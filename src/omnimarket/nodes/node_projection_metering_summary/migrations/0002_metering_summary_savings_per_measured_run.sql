-- OMN-20009: the average saving per measured run, served beside savings_usd.
--
-- The Overview's "Avg saving / call" is savings over calls from this one
-- savings definition, and the dashboard may not divide. The fold writes
-- savings_usd / runs_measured as decimal text to the millionth, and NULL --
-- never zero -- when savings_usd is NULL (no measured run, or no resolved
-- baseline). Nullable with no default, so existing rows read NULL until the
-- next refresh replaces them.

ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS savings_per_measured_run_usd TEXT;
