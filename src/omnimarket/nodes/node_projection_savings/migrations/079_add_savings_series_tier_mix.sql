-- OMN-13661 (T2): tier-mix percentages for the delegation savings-over-time
-- series. Extends projection_delegation_savings_series (migration 078) with the
-- per-bucket routing-tier distribution the dashboard renders alongside the
-- cost/savings curve, WITHOUT re-deriving tier from a model-name regex.
--
-- cost_tier_name is authoritative on delegation_events as of OMN-13649
-- (migration 0018 column + the #1458 write path: the orchestrator now carries
-- workflow.current_tier_name onto the canonical terminal, so a COMPLETED
-- local/free delegation records its real serving tier instead of '').
--
-- Authoritative tier -> bucket mapping (routing_tiers.yaml escalation ladder
-- local -> cheap_cloud -> cheap_frontier -> claude, collapsed to three display
-- buckets). The projection owns this rule so the UI consumes it, never re-derives:
--     'local'                         -> local   (owned-GPU, free_local cost)
--     'cheap_cloud' | 'cheap_frontier'-> cheap   (cheap/free cloud, mid-ladder)
--     'claude'                        -> premium (ceiling tier slot)
--     '' / NULL / unrecognized        -> not_tier_routed (no cost_tier_name)
--
-- not_tier_routed rows (savings_estimates rows without a matching tier-routed
-- delegation event, plus any delegation_events terminal that predates
-- OMN-13649 and never carried a tier) are EXCLUDED from the tier-% denominator
-- but still counted in task_count, per the T3/OMN-13662 rule. Each pct =
-- count(bucket rows) / count(tier-routed rows) within the day; the three pcts
-- therefore sum to 1.0 for any bucket that has at least one tier-routed row, and
-- are 0 when a bucket has none.
--
-- CREATE OR REPLACE keeps the existing five columns (bucket, actual_cost_usd,
-- baseline_cost_usd, savings_usd, task_count) in the same order and type and
-- appends local_pct/cheap_pct/prem_pct, satisfying Postgres' replace-view rule.

CREATE OR REPLACE VIEW projection_delegation_savings_series AS
WITH savings_sessions AS (
    SELECT
        session_id,
        model_local AS task_type,
        model_local AS model_name,
        local_cost_usd::float AS local_cost_usd,
        cloud_cost_usd::float AS cloud_cost_usd,
        savings_usd::float AS savings_usd,
        model_cloud_baseline AS baseline_model,
        'savings-estimated' AS pricing_manifest_version,
        'measured' AS savings_method,
        'savings_estimates' AS usage_source,
        0::int AS prompt_tokens,
        0::int AS completion_tokens,
        NULL::int AS tokens_to_compliance,
        NULL::int AS latency_ms,
        created_at,
        NULL::text AS prompt_text,
        NULL::text AS response_text,
        -- savings_estimates rows are not LLM-tier-routed: no serving tier.
        NULL::text AS cost_tier_name
    FROM savings_estimates
),
event_sessions AS (
    SELECT
        COALESCE(NULLIF(session_id, ''), NULLIF(correlation_id, ''), id::text)
            AS session_id,
        COALESCE(task_type, '') AS task_type,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'local')
            AS model_name,
        COALESCE(cost_usd, 0)::float AS local_cost_usd,
        (COALESCE(cost_usd, 0) + COALESCE(cost_savings_usd, 0))::float
            AS cloud_cost_usd,
        COALESCE(cost_savings_usd, 0)::float AS savings_usd,
        'claude-opus-4.1' AS baseline_model,
        pricing_manifest_version::text AS pricing_manifest_version,
        CASE WHEN COALESCE(cost_savings_usd, 0) > 0
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE WHEN COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0) > 0
            THEN 'measured' ELSE 'unknown' END AS usage_source,
        COALESCE(tokens_input, 0)::int AS prompt_tokens,
        COALESCE(tokens_output, 0)::int AS completion_tokens,
        NULLIF(tokens_to_compliance, 0)::int AS tokens_to_compliance,
        COALESCE(delegation_latency_ms, latency_ms)::int AS latency_ms,
        COALESCE(created_at, timestamp) AS created_at,
        prompt_text,
        response_text,
        -- OMN-13649: authoritative serving tier. Empty string -> not tier-routed.
        NULLIF(cost_tier_name, '') AS cost_tier_name
    FROM delegation_events
),
event_tiers AS (
    SELECT
        session_id,
        (array_agg(cost_tier_name ORDER BY created_at DESC)
            FILTER (WHERE cost_tier_name IS NOT NULL))[1] AS cost_tier_name
    FROM event_sessions
    GROUP BY session_id
),
combined_sessions AS (
    SELECT
        savings_sessions.session_id,
        savings_sessions.task_type,
        savings_sessions.model_name,
        savings_sessions.local_cost_usd,
        savings_sessions.cloud_cost_usd,
        savings_sessions.savings_usd,
        savings_sessions.baseline_model,
        savings_sessions.pricing_manifest_version,
        savings_sessions.savings_method,
        savings_sessions.usage_source,
        savings_sessions.prompt_tokens,
        savings_sessions.completion_tokens,
        savings_sessions.tokens_to_compliance,
        savings_sessions.latency_ms,
        savings_sessions.created_at,
        savings_sessions.prompt_text,
        savings_sessions.response_text,
        COALESCE(event_tiers.cost_tier_name, savings_sessions.cost_tier_name)
            AS cost_tier_name
    FROM savings_sessions
    LEFT JOIN event_tiers USING (session_id)
    UNION ALL
    SELECT event_sessions.*
    FROM event_sessions
    WHERE NOT EXISTS (
        SELECT 1
        FROM savings_sessions
        WHERE savings_sessions.session_id = event_sessions.session_id
    )
),
classified_sessions AS (
    SELECT
        *,
        CASE
            WHEN cost_tier_name = 'local' THEN 'local'
            WHEN cost_tier_name IN ('cheap_cloud', 'cheap_frontier') THEN 'cheap'
            WHEN cost_tier_name = 'claude' THEN 'premium'
            ELSE NULL  -- not_tier_routed: excluded from the tier-% denominator
        END AS tier_bucket
    FROM combined_sessions
)
SELECT
    date_trunc('day', created_at) AS bucket,
    COALESCE(SUM(local_cost_usd), 0)::float AS actual_cost_usd,
    COALESCE(SUM(cloud_cost_usd), 0)::float AS baseline_cost_usd,
    COALESCE(SUM(savings_usd), 0)::float AS savings_usd,
    COUNT(*)::int AS task_count,
    COALESCE(
        COUNT(*) FILTER (WHERE tier_bucket = 'local')::float
        / NULLIF(COUNT(*) FILTER (WHERE tier_bucket IS NOT NULL), 0),
        0
    )::float AS local_pct,
    COALESCE(
        COUNT(*) FILTER (WHERE tier_bucket = 'cheap')::float
        / NULLIF(COUNT(*) FILTER (WHERE tier_bucket IS NOT NULL), 0),
        0
    )::float AS cheap_pct,
    COALESCE(
        COUNT(*) FILTER (WHERE tier_bucket = 'premium')::float
        / NULLIF(COUNT(*) FILTER (WHERE tier_bucket IS NOT NULL), 0),
        0
    )::float AS prem_pct
FROM classified_sessions
GROUP BY 1
ORDER BY 1;
