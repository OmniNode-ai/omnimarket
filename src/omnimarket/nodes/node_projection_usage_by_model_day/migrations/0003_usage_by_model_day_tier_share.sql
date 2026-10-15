-- OMN-21015: each usage row carries its tier, and its share of its tenant's calls that day.
-- Owner: omnimarket.nodes.node_projection_usage_by_model_day
-- Target database: omnidash_analytics; physical schema: public.
--
-- The tier joins the aggregate's key, so one model served under two tiers on one
-- day is two rows. A call or row stored before this migration names no tier and
-- reads not_tier_routed, the name the delegation tier distribution gives a run
-- with no tier (migration 0021 of node_projection_delegation). call_share is the
-- row's call_count over the summed call_count of its tenant and day; the writer
-- recomputes it for every row of the day on each new call. A row stored before
-- this migration reads call_share NULL, not computed, until its day takes a new
-- call, as migration 0002 leaves measured_cost_usd: a backfill here would run
-- under the table's forced row security and update nothing for this role.
-- published_cursor is the projection_cursor the writer last confirmed it sent for
-- the row; a replayed call sends only rows where the two differ. It is not served.
-- A row stored before this migration reads NULL, so a replay of an older call
-- sends that row once more.
-- Re-runnable: each step checks what it changes.

ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS cost_tier_name TEXT NOT NULL DEFAULT 'not_tier_routed';
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS cost_tier_name TEXT NOT NULL DEFAULT 'not_tier_routed';
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS call_share DOUBLE PRECISION CHECK (call_share >= 0 AND call_share <= 1);
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS published_cursor BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS c
        JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = 'public.usage_by_model_day'::regclass
          AND c.contype = 'p'
          AND a.attname = 'cost_tier_name'
    ) THEN
        ALTER TABLE public.usage_by_model_day DROP CONSTRAINT usage_by_model_day_pkey;
        ALTER TABLE public.usage_by_model_day
            ADD CONSTRAINT usage_by_model_day_pkey PRIMARY KEY (tenant_id, usage_day, model_id, cost_tier_name);
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_usage_by_model_day_calls_tier_key
    ON public.usage_by_model_day_calls (tenant_id, usage_day, model_id, cost_tier_name);
