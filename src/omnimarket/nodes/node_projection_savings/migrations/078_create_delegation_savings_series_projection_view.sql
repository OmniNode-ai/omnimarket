-- OMN-13648 (G1): time-bucketed savings-over-time series for the delegation
-- dashboard "savings over time" depth view.
--
-- The new omnidash delegation dashboard (OMN-13602) renders a savings-over-time
-- chart, but no projection exposes a per-bucket series. This view re-derives the
-- same bus-fed delegation source as projection_delegation_savings (migration
-- 076) -- the UNION of savings_estimates and delegation_events -- then
-- aggregates it into ONE row per UTC day across ALL sessions (no LIMIT 500).
--
-- The source CTEs (savings_sessions / event_sessions / combined_sessions) are
-- copied faithfully from migration 076 so this series view stands alone and does
-- NOT depend on 076's internal CTEs.
--
-- Tier-mix columns (local_pct / cheap_pct / prem_pct) are intentionally deferred
-- to OMN-13649; this view ships only the cost/savings/task_count aggregates.

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
        NULL::text AS response_text
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
        response_text
    FROM delegation_events
),
combined_sessions AS (
    SELECT * FROM savings_sessions
    UNION ALL
    SELECT event_sessions.*
    FROM event_sessions
    WHERE NOT EXISTS (
        SELECT 1
        FROM savings_sessions
        WHERE savings_sessions.session_id = event_sessions.session_id
    )
)
SELECT
    date_trunc('day', created_at) AS bucket,
    COALESCE(SUM(local_cost_usd), 0)::float AS actual_cost_usd,
    COALESCE(SUM(cloud_cost_usd), 0)::float AS baseline_cost_usd,
    COALESCE(SUM(savings_usd), 0)::float AS savings_usd,
    COUNT(*)::int AS task_count
FROM combined_sessions
GROUP BY 1
ORDER BY 1;
