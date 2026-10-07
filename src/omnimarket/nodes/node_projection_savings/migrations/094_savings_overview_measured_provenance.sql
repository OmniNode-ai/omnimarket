-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-20008: a counterfactual is modelled; measured totals require persisted
-- measured provenance. Unknown and estimated runs remain visible and counted.
-- This forward-only replacement preserves all 17 existing columns and grants.
-- The savings row owns the numeric fields, so it must also own their basis.

CREATE OR REPLACE VIEW public.projection_cost_savings_overview AS
WITH raw_savings_runs AS (
    SELECT
        CASE WHEN tenant_id = 'omninode' THEN '820272f9-4aaf-5add-a2df-0af942852ab2'
             ELSE tenant_id::text END AS tenant_id,
        session_id AS correlation_id,
        session_id,
        COALESCE(NULLIF(task_type, ''), 'savings-estimated') AS task_type,
        COALESCE(NULLIF(model_local, ''), 'local') AS model_id,
        COALESCE(NULLIF(model_local, ''), 'Local model') AS display_name,
        local_cost_usd::float AS cost_usd,
        cloud_cost_usd::float AS baseline_cost_usd,
        savings_usd::float AS savings_usd,
        COALESCE(prompt_tokens, 0)::int AS prompt_tokens,
        COALESCE(completion_tokens, 0)::int AS completion_tokens,
        NULL::int AS tokens_to_compliance,
        NULL::int AS latency_ms,
        NULL::boolean AS quality_gate_passed,
        NULL::text AS cost_tier_name,
        COALESCE(usage_source, 'unknown') AS token_provenance,
        COALESCE(updated_at, created_at, event_timestamp)::timestamptz
            AS projected_at
    FROM public.savings_estimates
    WHERE tenant_id IS NOT NULL
      AND session_id IS NOT NULL
),
savings_runs AS (
    SELECT
        tenant_id, correlation_id, session_id, task_type, model_id, display_name,
        cost_usd, baseline_cost_usd, savings_usd, prompt_tokens,
        completion_tokens, tokens_to_compliance, latency_ms, quality_gate_passed,
        cost_tier_name, token_provenance, projected_at
    FROM (
        SELECT
            raw_savings_runs.*,
            ROW_NUMBER() OVER (
                PARTITION BY tenant_id, correlation_id
                ORDER BY projected_at DESC, session_id DESC
            ) AS tenant_run_rank
        FROM raw_savings_runs
    ) ranked
    WHERE tenant_run_rank = 1
),
event_runs AS (
    SELECT
        tenant_id::text AS tenant_id,
        COALESCE(NULLIF(correlation_id, ''), NULLIF(session_id, ''), id::text)
            AS correlation_id,
        COALESCE(NULLIF(session_id, ''), NULLIF(correlation_id, ''), id::text)
            AS session_id,
        COALESCE(NULLIF(task_type, ''), 'delegation') AS task_type,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'local')
            AS model_id,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'Local model')
            AS display_name,
        COALESCE(cost_usd, 0)::float AS cost_usd,
        (COALESCE(cost_usd, 0) + COALESCE(cost_savings_usd, 0))::float
            AS baseline_cost_usd,
        COALESCE(cost_savings_usd, 0)::float AS savings_usd,
        COALESCE(tokens_input, 0)::int AS prompt_tokens,
        COALESCE(tokens_output, 0)::int AS completion_tokens,
        NULLIF(tokens_to_compliance, 0)::int AS tokens_to_compliance,
        COALESCE(delegation_latency_ms, latency_ms)::int AS latency_ms,
        quality_gate_passed,
        cost_tier_name::text AS cost_tier_name,
        CASE
            WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split') THEN 'measured'
            WHEN cost_measurement_source = 'manifest_compute' THEN 'estimated'
            ELSE 'unknown'
        END AS token_provenance,
        COALESCE(created_at, timestamp)::timestamptz AS projected_at
    FROM public.delegation_events
    WHERE tenant_id IS NOT NULL
),
combined_runs AS (
    SELECT
        event_runs.tenant_id,
        event_runs.correlation_id,
        event_runs.session_id,
        event_runs.task_type,
        event_runs.model_id,
        event_runs.display_name,
        COALESCE(savings_runs.cost_usd, event_runs.cost_usd) AS cost_usd,
        COALESCE(savings_runs.baseline_cost_usd, event_runs.baseline_cost_usd)
            AS baseline_cost_usd,
        COALESCE(savings_runs.savings_usd, event_runs.savings_usd)
            AS savings_usd,
        COALESCE(NULLIF(savings_runs.prompt_tokens, 0), event_runs.prompt_tokens)
            AS prompt_tokens,
        COALESCE(
            NULLIF(savings_runs.completion_tokens, 0),
            event_runs.completion_tokens
        ) AS completion_tokens,
        event_runs.tokens_to_compliance,
        event_runs.latency_ms,
        event_runs.quality_gate_passed,
        event_runs.cost_tier_name,
        CASE WHEN savings_runs.correlation_id IS NOT NULL
            THEN savings_runs.token_provenance
            ELSE event_runs.token_provenance
        END AS token_provenance,
        COALESCE(
            GREATEST(event_runs.projected_at, savings_runs.projected_at),
            event_runs.projected_at,
            savings_runs.projected_at
        )
            AS projected_at
    FROM event_runs
    LEFT JOIN savings_runs
      ON savings_runs.correlation_id = event_runs.correlation_id
     AND savings_runs.tenant_id = event_runs.tenant_id
    UNION ALL
    SELECT savings_runs.*
    FROM savings_runs
    WHERE NOT EXISTS (
        SELECT 1
        FROM event_runs
        WHERE event_runs.correlation_id = savings_runs.correlation_id
          AND event_runs.tenant_id = savings_runs.tenant_id
    )
),
totals AS (
    SELECT
        tenant_id,
        (SUM(cost_usd) FILTER (WHERE token_provenance = 'measured'))::float AS total_cost_usd,
        (SUM(baseline_cost_usd) FILTER (WHERE token_provenance = 'measured'))::float AS total_baseline_cost_usd,
        (SUM(savings_usd) FILTER (WHERE token_provenance = 'measured'))::float AS total_savings_usd,
        COALESCE(SUM(prompt_tokens + completion_tokens) FILTER (WHERE token_provenance = 'measured'), 0)::int AS tokens_total,
        COALESCE(SUM(COALESCE(tokens_to_compliance, 0)) FILTER (WHERE token_provenance = 'measured'), 0)::int
            AS tokens_to_compliance,
        COUNT(*) FILTER (
            WHERE token_provenance = 'measured'
        )::int AS measured_run_count,
        COUNT(*) FILTER (WHERE token_provenance = 'estimated')::int
            AS estimated_run_count,
        COUNT(*) FILTER (WHERE token_provenance = 'unknown')::int
            AS unknown_run_count,
        COUNT(*) FILTER (
            WHERE prompt_tokens + completion_tokens = 0
        )::int AS zero_token_run_count,
        COUNT(*)::int AS run_count,
        COALESCE(SUM(prompt_tokens + completion_tokens) FILTER (
            WHERE token_provenance = 'measured' AND cost_tier_name = 'local'
        ), 0)::float AS local_tier_tokens,
        COALESCE(SUM(prompt_tokens + completion_tokens) FILTER (
            WHERE token_provenance = 'measured' AND cost_tier_name IN (
                'local', 'cheap_cloud', 'cheap_frontier', 'claude'
            )
        ), 0)::float AS tiered_tokens,
        MAX(projected_at) AS latest_projection_updated_at
    FROM combined_runs
    GROUP BY tenant_id
),
model_rows AS (
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'model_id', model_id,
                    'display_name', display_name,
                    'execution_mode', 'delegated',
                    'task_count', task_count,
                    'tokens_total', tokens_total,
                    'cost_usd', cost_usd,
                    'baseline_cost_usd', baseline_cost_usd,
                    'savings_usd', savings_usd,
                    'savings_pct', CASE WHEN baseline_cost_usd IS NULL THEN NULL
                        WHEN baseline_cost_usd > 0
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
            tenant_id,
            model_id,
            display_name,
            COUNT(*)::int AS task_count,
            COALESCE(SUM(prompt_tokens + completion_tokens) FILTER (WHERE token_provenance = 'measured'), 0)::int
                AS tokens_total,
            (SUM(cost_usd) FILTER (WHERE token_provenance = 'measured'))::float AS cost_usd,
            (SUM(baseline_cost_usd) FILTER (WHERE token_provenance = 'measured'))::float AS baseline_cost_usd,
            (SUM(savings_usd) FILTER (WHERE token_provenance = 'measured'))::float AS savings_usd
        FROM combined_runs
        GROUP BY tenant_id, model_id, display_name
    ) grouped_models
    GROUP BY tenant_id
),
ranked_runs AS (
    SELECT
        combined_runs.*,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id
            ORDER BY projected_at DESC, correlation_id DESC, session_id DESC
        ) AS tenant_rank
    FROM combined_runs
),
recent_runs AS (
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'session_id', session_id,
                    'task_type', task_type,
                    'model_name', display_name,
                    'prompt_tokens', prompt_tokens,
                    'completion_tokens', completion_tokens,
                    'total_tokens', prompt_tokens + completion_tokens,
                    'savings_usd', savings_usd,
                    'latency_ms', latency_ms,
                    'created_at', projected_at,
                    'token_provenance', token_provenance,
                    'correlation_id', correlation_id,
                    'cost_usd', cost_usd,
                    'cost_savings_usd', savings_usd,
                    'quality_gate_passed', quality_gate_passed,
                    'tokens_to_compliance', tokens_to_compliance,
                    'cost_tier_name', cost_tier_name
                )
                ORDER BY projected_at DESC, correlation_id DESC, session_id DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM ranked_runs
    WHERE tenant_rank <= 20
    GROUP BY tenant_id
),
warnings AS (
    SELECT tenant_id,
        CASE WHEN estimated_run_count + unknown_run_count > 0
            THEN jsonb_build_array(
                estimated_run_count || ' estimated and ' || unknown_run_count
                || ' unknown run(s) excluded; totals use measured runs'
            ) ELSE '[]'::jsonb END
        || CASE WHEN zero_token_run_count > 0 THEN jsonb_build_array(
            zero_token_run_count || ' run(s) carry no served-token counts;'
            || ' measured totals follow stored provenance, not token presence'
        ) ELSE '[]'::jsonb END
        || CASE WHEN tokens_total > 0 AND tiered_tokens = 0 THEN jsonb_build_array(
            'No measured run carries a serving tier; local_token_pct is'
            || ' unmeasured and reported as zero'
        ) ELSE '[]'::jsonb END AS rows
    FROM totals
)
SELECT
    'all'::text AS "window",
    totals.total_cost_usd,
    totals.total_baseline_cost_usd,
    totals.total_savings_usd,
    CASE WHEN totals.total_baseline_cost_usd IS NULL THEN NULL
        WHEN totals.total_baseline_cost_usd > 0
        THEN totals.total_savings_usd / totals.total_baseline_cost_usd
        ELSE 0
    END AS savings_rate,
    totals.tokens_total,
    totals.tokens_to_compliance,
    COALESCE(totals.local_tier_tokens / NULLIF(totals.tiered_tokens, 0), 0)::float
        AS local_token_pct,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    COALESCE(model_rows.rows, '[]'::jsonb) AS rows,
    COALESCE(recent_runs.rows, '[]'::jsonb) AS recent_runs,
    totals.measured_run_count,
    totals.zero_token_run_count,
    COALESCE(warnings.rows, '[]'::jsonb) AS warnings,
    (totals.run_count > 0) AS provisioned,
    totals.latest_projection_updated_at,
    totals.tenant_id,
    totals.estimated_run_count,
    totals.unknown_run_count
FROM totals
LEFT JOIN model_rows ON model_rows.tenant_id = totals.tenant_id
LEFT JOIN recent_runs ON recent_runs.tenant_id = totals.tenant_id
LEFT JOIN warnings ON warnings.tenant_id = totals.tenant_id;

ALTER VIEW public.projection_cost_savings_overview SET (security_invoker = true);
GRANT SELECT ON public.projection_cost_savings_overview TO app_dashboard;
GRANT SELECT ON public.projection_cost_savings_overview TO tenant_projection_writer;
