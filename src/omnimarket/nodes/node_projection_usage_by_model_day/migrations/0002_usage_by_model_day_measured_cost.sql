-- OMN-20006: separate measured cost from cost that was estimated or not recorded.
-- Owner: omnimarket.nodes.node_projection_usage_by_model_day
-- Target database: omnidash_analytics; physical schema: public.
--
-- Additive only. A call stored before this migration reads usage_source
-- 'unknown'; an aggregate row reads measured_cost_usd NULL and
-- unmeasured_call_count 0 until the next call for its key recounts it. Nothing
-- is rewritten, so old code keeps working and a rollback needs no data change.
-- Re-runnable: every statement is IF NOT EXISTS.

ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS usage_source TEXT NOT NULL DEFAULT 'unknown';

-- NULL means no call for the key had a measured cost; it is never written as 0.
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS measured_cost_usd NUMERIC(18,8) CHECK (measured_cost_usd >= 0);
ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS unmeasured_call_count INTEGER NOT NULL DEFAULT 0 CHECK (unmeasured_call_count >= 0);
