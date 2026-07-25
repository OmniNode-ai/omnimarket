-- OMN-15038: drop routing_rule / routing_confidence / routing_candidates from
-- delegation_events -- speculative schema, never wired to any write path.
--
-- INVESTIGATION (2026-07-25):
--   * No migration in this repo (node-scoped 0007-0023 here, nor the flat
--     omnibase_infra/docker/migrations/forward/*.sql sequence) ever creates
--     these three columns. They exist on the live omnidash_analytics DB on
--     the stability-test lane as pure schema drift (someone ran an ad-hoc
--     ALTER TABLE outside the migration tree). The dev lane's
--     omnidash_analytics.delegation_events does NOT have the columns at all
--     -- confirmed live via information_schema.columns on both lanes.
--   * Neither write path in handler_projection_delegation.py
--     (HandlerProjectionDelegation.project() / .project_delegate_skill_terminal())
--     ever sets a routing_rule/routing_confidence/routing_candidates key on the
--     upsert row dict. No event model in omnimarket.nodes.node_projection_delegation
--     or omnimarket.models.delegation carries these fields either.
--   * Both read paths that surface these field NAMES in dashboard payloads
--     (0010_create_delegation_dashboard_projection_views.sql /
--     0021_delegation_tier_distribution_not_tier_routed.sql decision_traces
--     jsonb_build_object, and omnidash/server/postgres-projection-reader.ts
--     onex.snapshot.projection.delegation.model-routing.v1 decision traces
--     query) hardcode `NULL` LITERALS -- they never SELECT the column, so
--     dropping it changes no observable behavior on any currently-deployed
--     read path.
--   * Live data on stability-test: 161 of 162 rows NULL; the single non-null
--     row (created_at 2026-05-31) predates any wired write path and does not
--     correlate with a real event -- a one-off manual/test INSERT, not proof
--     of a working feature.
--   * The nearest live analog of "which routing decision served this task" is
--     delegation_events.cost_tier_name (migration 0018, "the routing tier
--     name that served the task"), which IS wired end-to-end from
--     ModelRoutingDecision.tier_name through the delegate-skill terminal and
--     canonical-result converters. A separate, unrelated "agent routing"
--     concept (which coding agent, not which LLM/tier) lands in the
--     omnibase_infra-owned agent_routing_decisions table via
--     onex.evt.omniclaude.routing-decision.v1 -- a different table, different
--     domain, and itself 0 rows on stability-test today. Neither is a live
--     source these 3 columns were ever wired to.
--
-- A permanently-NULL column that no migration created and no writer touches
-- is worse than no column: it implies data that will never arrive. Drop it.
-- IF EXISTS makes this a no-op on the dev lane (columns never existed there)
-- and a real drop on stability-test (schema drift cleanup). No dependent view
-- or index references the real column (verified above), so no CASCADE is
-- required.

ALTER TABLE delegation_events
    DROP COLUMN IF EXISTS routing_rule,
    DROP COLUMN IF EXISTS routing_confidence,
    DROP COLUMN IF EXISTS routing_candidates;
