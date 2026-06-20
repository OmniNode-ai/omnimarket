-- OMN-13234: persist the typed per-tier actual-cost measurement on
-- delegation_events rows so honest savings (counterfactual - actual) is
-- auditable and recomputable from the projection.
--
-- The typed tier cost model (free_local | metered | budgeted, in
-- omnibase_core ModelTierCost / EnumTierCostType) decides how a tier is billed.
-- When a cheaper tier serves a task the premium model never runs, so the
-- premium_counterfactual (added in 0017) is a modeled figure, not a measurement.
-- These columns carry the OTHER half of the saving — the tier's actual cost:
--
--   cost_tier_type           the typed cost regime that priced this call
--                            (free_local | metered | budgeted)
--   cost_tier_name           the routing tier name that served the task
--                            (local | cheap_cloud | cheap_frontier | claude)
--   cost_measurement_source  how cost_usd was derived
--                            (free_local | metered | budgeted_in_budget |
--                             budgeted_overage | budgeted_split |
--                             manifest_compute | no_cost_model)
--   budget_headroom_consumed_usd  for budgeted tiers, the monthly-cap drawdown
--                            (accounting cost of in-budget tokens; 0 cash)
--
-- cost_usd (cash) and cost_savings_usd already exist (0007); this migration
-- only records HOW cost_usd was measured. cost_savings_usd is computed at write
-- time as premium_counterfactual.counterfactual_cost_usd - cost_usd.
--
-- Idempotent ADD COLUMN IF NOT EXISTS so warm dev/stability volumes that
-- already contain delegation_events reconcile cleanly.

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS cost_tier_type TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS cost_tier_name TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS cost_measurement_source TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS budget_headroom_consumed_usd NUMERIC NOT NULL DEFAULT 0;
