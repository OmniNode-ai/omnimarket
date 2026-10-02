-- OMN-19969: the series view must not invent a savings baseline for legacy
-- delegation_events. That table does not persist model_cloud_baseline; only
-- savings_estimates does. Keep the recorded value on savings rows and represent
-- the event-only value as NULL until a producer persists its selected baseline.
-- Do not include an event-only row in the savings series without that value;
-- otherwise a numeric cost_savings_usd would appear against an unknown baseline.
--
-- 090 leaves this series view untouched, so 087's definition remained active.
-- This forward replacement keeps the existing output contract and tier buckets.

CREATE OR REPLACE VIEW public.projection_delegation_savings_series AS
WITH savings_sessions AS (
    SELECT
        session_id,
        COALESCE(task_type, '') AS task_type,
        model_local AS model_name,
        local_cost_usd::float AS local_cost_usd,
        cloud_cost_usd::float AS cloud_cost_usd,
        savings_usd::float AS savings_usd,
        model_cloud_baseline AS baseline_model,
        COALESCE(pricing_manifest_version, 'savings-estimated')
            AS pricing_manifest_version,
        COALESCE(savings_method, 'estimated') AS savings_method,
        COALESCE(usage_source, 'unknown') AS usage_source,
        prompt_tokens::int AS prompt_tokens,
        completion_tokens::int AS completion_tokens,
        NULL::int AS tokens_to_compliance,
        NULL::int AS latency_ms,
        created_at,
        NULL::text AS prompt_text,
        NULL::text AS response_text,
        NULL::text AS cost_tier_name
    FROM public.savings_estimates
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
        NULL::text AS baseline_model,
        pricing_manifest_version::text AS pricing_manifest_version,
        CASE WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split')
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE
            WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split') THEN 'measured'
            WHEN cost_measurement_source = 'manifest_compute' THEN 'estimated'
            ELSE 'unknown'
        END AS usage_source,
        COALESCE(tokens_input, 0)::int AS prompt_tokens,
        COALESCE(tokens_output, 0)::int AS completion_tokens,
        NULLIF(tokens_to_compliance, 0)::int AS tokens_to_compliance,
        COALESCE(delegation_latency_ms, latency_ms)::int AS latency_ms,
        COALESCE(created_at, timestamp) AS created_at,
        prompt_text,
        response_text,
        NULLIF(cost_tier_name, '') AS cost_tier_name
    FROM public.delegation_events
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
      AND event_sessions.baseline_model IS NOT NULL
),
classified_sessions AS (
    SELECT
        *,
        CASE
            WHEN cost_tier_name = 'local' THEN 'local'
            WHEN cost_tier_name IN ('cheap_cloud', 'cheap_frontier') THEN 'cheap'
            WHEN cost_tier_name = 'claude' THEN 'premium'
            ELSE NULL
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

-- CREATE OR REPLACE VIEW resets omitted view options, so the replacement above has
-- dropped security_invoker (OMN-19808: the same reset after 089 and 090). Without it
-- the view reads with its owner's rights and bypasses the tenant row-level security
-- on savings_estimates and delegation_events. Set it back in the same migration.
ALTER VIEW public.projection_delegation_savings_series SET (security_invoker = true);
