-- OMN-20008: savings as a share of the baseline, served beside savings_usd.
--
-- The Overview's "N% below the <baseline> baseline (modelled)" line may not
-- divide money in the browser. The fold writes savings_usd / counterfactual_usd
-- as decimal text to the millionth, and NULL -- never zero -- when either is
-- NULL or counterfactual_usd is 0. Nullable with no default, so existing rows
-- read NULL until the next refresh replaces them.

ALTER TABLE public.metering_summary
    ADD COLUMN IF NOT EXISTS savings_pct_of_counterfactual TEXT;
