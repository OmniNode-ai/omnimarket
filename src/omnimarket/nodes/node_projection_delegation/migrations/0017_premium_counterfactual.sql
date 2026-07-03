-- OMN-13355: persist the pinned premium counterfactual on delegation_events rows.
--
-- When a cheaper tier serves a delegated task the premium (frontier) model never
-- runs, so the saving is a modeled counterfactual, not a measurement. The
-- projection now carries the pinned counterfactual provenance
-- {model, price_in_per_1k, price_out_per_1k, as_of, tokens_in, tokens_out,
-- counterfactual_cost_usd, pricing_source, measured} as a single JSONB column so
-- the saving (cost_savings_usd = counterfactual_cost_usd - cost_usd) is auditable
-- and recomputable from the persisted provenance. There is no live premium API
-- call — the price/as_of come from the canonical pricing manifest.
--
-- Idempotent ADD COLUMN IF NOT EXISTS so warm dev/stability volumes that already
-- contain delegation_events reconcile cleanly.

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS premium_counterfactual JSONB;
