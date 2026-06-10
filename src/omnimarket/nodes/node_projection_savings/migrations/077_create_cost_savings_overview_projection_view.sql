-- OMN-12839: dashboard cost-savings overview snapshot over savings_estimates.
--
-- The dashboard overview widget consumes one composed row with KPI totals,
-- per-model savings rows, recent runs, and warning metadata. Compose that view
-- next to the savings reducer so the projection API can serve the widget from a
-- contract-declared read model instead of fixture-only data.

CREATE OR REPLACE VIEW projection_cost_savings_overview AS
WITH source_rows AS (
    SELECT
        session_id,
        COALESCE(NULLIF(model_local, ''), 'local') AS model_id,
        COALESCE(NULLIF(model_local, ''), 'Local model') AS display_name,
        COALESCE(NULLIF(model_cloud_baseline, ''), 'cloud-baseline')
            AS baseline_model,
        local_cost_usd::float AS local_cost_usd,
        cloud_cost_usd::float AS cloud_cost_usd,
        savings_usd::float AS savings_usd,
        COALESCE(updated_at, created_at, event_timestamp) AS projected_at
    FROM savings_estimates
),
totals AS (
    SELECT
        COALESCE(SUM(local_cost_usd), 0)::float AS total_cost_usd,
        COALESCE(SUM(cloud_cost_usd), 0)::float AS total_baseline_cost_usd,
        COALESCE(SUM(savings_usd), 0)::float AS total_savings_usd,
        COUNT(*)::int AS run_count,
        MAX(projected_at) AS latest_projection_updated_at
    FROM source_rows
),
model_rows AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'model_id', model_id,
                'display_name', display_name,
                'execution_mode', 'delegated',
                'task_count', task_count,
                'tokens_total', 0,
                'cost_usd', cost_usd,
                'baseline_cost_usd', baseline_cost_usd,
                'savings_usd', savings_usd,
                'savings_pct', CASE WHEN baseline_cost_usd > 0
                    THEN savings_usd / baseline_cost_usd ELSE 0 END,
                'runtime_address', NULL,
                'evidence_ref', NULL
            )
            ORDER BY savings_usd DESC, display_name
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT
            model_id,
            display_name,
            COUNT(*)::int AS task_count,
            COALESCE(SUM(local_cost_usd), 0)::float AS cost_usd,
            COALESCE(SUM(cloud_cost_usd), 0)::float AS baseline_cost_usd,
            COALESCE(SUM(savings_usd), 0)::float AS savings_usd
        FROM source_rows
        GROUP BY model_id, display_name
    ) grouped_models
),
recent_runs AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'session_id', session_id,
                'task_type', 'savings-estimated',
                'model_name', display_name,
                'prompt_tokens', 0,
                'completion_tokens', 0,
                'total_tokens', 0,
                'savings_usd', savings_usd,
                'latency_ms', NULL,
                'created_at', projected_at,
                'token_provenance', 'unknown'
            )
            ORDER BY projected_at DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT *
        FROM source_rows
        ORDER BY projected_at DESC
        LIMIT 20
    ) limited_runs
),
warnings AS (
    SELECT CASE WHEN totals.run_count > 0 THEN
        jsonb_build_array(
            'Token counts unavailable in savings_estimates; token KPIs remain zero until measured token provenance is projected.'
        )
    ELSE '[]'::jsonb END AS rows
    FROM totals
)
SELECT
    'all'::text AS "window",
    totals.total_cost_usd,
    totals.total_baseline_cost_usd,
    totals.total_savings_usd,
    CASE WHEN totals.total_baseline_cost_usd > 0
        THEN totals.total_savings_usd / totals.total_baseline_cost_usd
        ELSE 0
    END AS savings_rate,
    0::int AS tokens_total,
    0::int AS tokens_to_compliance,
    0::float AS local_token_pct,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    model_rows.rows,
    recent_runs.rows AS recent_runs,
    0::int AS measured_run_count,
    totals.run_count AS zero_token_run_count,
    warnings.rows AS warnings,
    (totals.run_count > 0) AS provisioned,
    totals.latest_projection_updated_at
FROM totals
CROSS JOIN model_rows
CROSS JOIN recent_runs
CROSS JOIN warnings;
