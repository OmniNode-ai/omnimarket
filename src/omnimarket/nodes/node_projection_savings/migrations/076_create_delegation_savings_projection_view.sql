-- OMN-12489: dashboard savings read view over bus-fed delegation projections.

CREATE OR REPLACE VIEW projection_delegation_savings AS
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
),
limited_sessions AS (
    SELECT *
    FROM combined_sessions
    ORDER BY created_at DESC
    LIMIT 500
),
totals AS (
    SELECT
        COALESCE(SUM(savings_usd), 0)::float AS cumulative_savings_usd,
        COALESCE(SUM(local_cost_usd), 0)::float AS cumulative_local_cost_usd,
        COALESCE(SUM(cloud_cost_usd), 0)::float AS cumulative_cloud_cost_usd,
        COUNT(*)::int AS session_count,
        MAX(created_at) AS latest_projection_updated_at
    FROM combined_sessions
),
sessions AS (
    SELECT COALESCE(
        jsonb_agg(to_jsonb(limited_sessions) ORDER BY created_at DESC),
        '[]'::jsonb
    ) AS rows
    FROM limited_sessions
),
latest AS (
    SELECT
        baseline_model,
        pricing_manifest_version
    FROM combined_sessions
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    totals.cumulative_savings_usd,
    totals.cumulative_local_cost_usd,
    totals.cumulative_cloud_cost_usd,
    COALESCE(latest.baseline_model, 'claude-opus-4.1') AS baseline_model,
    COALESCE(latest.pricing_manifest_version, 'runtime-delegation-events')
        AS pricing_manifest_version,
    totals.session_count,
    sessions.rows AS sessions,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at
FROM totals
CROSS JOIN sessions
LEFT JOIN latest ON TRUE;
