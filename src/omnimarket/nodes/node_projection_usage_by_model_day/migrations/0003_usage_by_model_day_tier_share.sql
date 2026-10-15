-- OMN-21015: each usage row carries its tier, and its share of its tenant's calls that day.
-- Owner: omnimarket.nodes.node_projection_usage_by_model_day
-- Target database: omnidash_analytics; physical schema: public.
--
-- Expand only (Jonah, OMN-21015, 2026-10-15): the three-column primary key
-- (tenant_id, usage_day, model_id) stays the key, so the writer that runs on
-- lanes today keeps writing. This adds the tier, a four-column unique index the
-- contract migration (OMN-21161) will make the key, and the writer's DELETE.
-- The new writer follows the key in force: while the three-column key is the
-- key it folds a model's day into one row under not_tier_routed, as today; once
-- the four-column index is the key it splits by tier and removes the folded row,
-- which is why it needs DELETE.
--
-- A call or row stored before this migration names no tier and reads
-- not_tier_routed, the name the delegation tier distribution gives a run with no
-- tier (migration 0021 of node_projection_delegation). call_share is the row's
-- call_count over the summed call_count of its tenant and day; the writer
-- recomputes it for every row of the day on each new call. A row stored before
-- this migration reads call_share NULL, not computed, until its day takes a new
-- call, as migration 0002 leaves measured_cost_usd: a backfill here would run
-- under the table's forced row security and update nothing for this role.
-- Re-runnable: each step checks what it changes.

ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS cost_tier_name TEXT NOT NULL DEFAULT 'not_tier_routed';
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS cost_tier_name TEXT NOT NULL DEFAULT 'not_tier_routed';
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS call_share DOUBLE PRECISION CHECK (call_share >= 0 AND call_share <= 1);

CREATE UNIQUE INDEX IF NOT EXISTS usage_by_model_day_tier_key
    ON public.usage_by_model_day (tenant_id, usage_day, model_id, cost_tier_name);

CREATE INDEX IF NOT EXISTS idx_usage_by_model_day_calls_tier_key
    ON public.usage_by_model_day_calls (tenant_id, usage_day, model_id, cost_tier_name);

-- Migration 0001 creates the writer role and fails without it, so on a lane the
-- role is there; the guard only keeps this file applicable on its own.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'tenant_projection_writer') THEN
        GRANT DELETE ON public.usage_by_model_day TO tenant_projection_writer;
    END IF;
END$$;
