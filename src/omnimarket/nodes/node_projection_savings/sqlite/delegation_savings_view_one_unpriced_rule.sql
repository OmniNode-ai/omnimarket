-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-25862: the revision after delegation_savings_view_priced_spend.sql (OMN-24615, revision 6). One change, nothing
-- else: a run's cost is unpriced by priced_spend's rule, the metering headline's, so both count the same runs. A cost
-- is spend only when the model that ran is in the pricing manifest (exact, then lower-case key) and, if the manifest
-- lists it as free, the cost is not positive; a run on a model the manifest does not list is unpriced at any cost,
-- $0 included. Money totals do not change (an excluded $0 adds nothing); only the unpriced count does.
--
-- Applied as the store step omn25862_delegation_savings_one_unpriced_rule after
-- omn24615_delegation_savings_priced_spend_view; a later revision is a new step.
CREATE VIEW projection_delegation_savings AS
WITH call_usage AS (
    SELECT
        correlation_id,
        CASE
            WHEN SUM(CASE WHEN usage_source IN ('measured', 'estimated')
                THEN 0 ELSE 1 END) > 0 THEN 'unknown'
            WHEN SUM(CASE WHEN usage_source = 'measured' THEN 1 ELSE 0 END)
                = COUNT(*) THEN 'measured'
            ELSE 'estimated'
        END AS usage_source
    FROM llm_call_metrics
    GROUP BY correlation_id
),
store_calls AS (
    SELECT EXISTS (SELECT 1 FROM llm_call_metrics) AS any_call
),
event_sessions AS (
    SELECT
        events.tenant_id AS tenant_id,
        COALESCE(NULLIF(events.correlation_id, ''), NULLIF(events.session_id, ''),
            CAST(events.id AS TEXT)) AS session_id,
        COALESCE(events.task_type, '') AS task_type,
        COALESCE(NULLIF(events.model_name, ''), NULLIF(events.delegated_to, ''), 'local')
            AS model_name,
        CAST(CASE WHEN COALESCE(pm_exact.free, pm_lower.free) IS NULL OR (events.cost_usd > 0 AND COALESCE(pm_exact.free, pm_lower.free) = 1) THEN NULL ELSE events.cost_usd END AS REAL) AS local_cost_usd,
        CAST(COALESCE(CASE WHEN COALESCE(pm_exact.free, pm_lower.free) IS NULL OR (events.cost_usd > 0 AND COALESCE(pm_exact.free, pm_lower.free) = 1) THEN NULL ELSE events.cost_usd END, 0) + COALESCE(CASE WHEN COALESCE(events.terminal_failure_cause, '') <> '' THEN 0.0
            ELSE CAST(events.modelled_savings_usd AS REAL) END, 0) AS REAL)
            AS cloud_cost_usd,
        CAST(COALESCE(CASE WHEN COALESCE(pm_exact.free, pm_lower.free) IS NULL OR (events.cost_usd > 0 AND COALESCE(pm_exact.free, pm_lower.free) = 1) THEN NULL ELSE events.cost_usd END, 0) + COALESCE(CASE WHEN COALESCE(events.terminal_failure_cause, '') <> '' THEN 0.0
            ELSE CAST(events.modelled_savings_usd AS REAL) END, 0) AS REAL)
            AS counterfactual_baseline_usd,
        CASE WHEN COALESCE(events.terminal_failure_cause, '') <> '' THEN 0.0
            ELSE CAST(events.modelled_savings_usd AS REAL) END AS savings_usd,
        CAST(events.savings_baseline_model AS TEXT) AS baseline_model,
        CAST(COALESCE(events.savings_pricing_manifest_version, events.pricing_manifest_version)
            AS TEXT) AS pricing_manifest_version,
        CASE WHEN COALESCE(call_usage.usage_source, CASE WHEN store_calls.any_call THEN 'unknown' ELSE 'measured' END) = 'measured'
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        COALESCE(call_usage.usage_source, CASE WHEN store_calls.any_call THEN 'unknown' ELSE 'measured' END) AS usage_source,
        CAST(COALESCE(events.tokens_input, 0) AS INTEGER) AS prompt_tokens,
        CAST(COALESCE(events.tokens_output, 0) AS INTEGER) AS completion_tokens,
        CAST(NULLIF(events.tokens_to_compliance, 0) AS INTEGER) AS tokens_to_compliance,
        CAST(COALESCE(events.delegation_latency_ms, events.latency_ms) AS INTEGER)
            AS latency_ms,
        COALESCE(events.created_at, events.timestamp) AS created_at,
        CASE WHEN (CASE WHEN COALESCE(pm_exact.free, pm_lower.free) IS NULL OR (events.cost_usd > 0 AND COALESCE(pm_exact.free, pm_lower.free) = 1) THEN NULL ELSE events.cost_usd END) IS NULL THEN 1 ELSE 0 END AS unpriced
    FROM delegation_events AS events
    LEFT JOIN local_priced_models AS pm_exact ON pm_exact.model_key = COALESCE(NULLIF(events.model_name, ''), NULLIF(events.delegated_to, ''), '')
    LEFT JOIN local_priced_models AS pm_lower ON pm_lower.model_key = lower(COALESCE(NULLIF(events.model_name, ''), NULLIF(events.delegated_to, ''), ''))
    LEFT JOIN call_usage ON call_usage.correlation_id = events.correlation_id
    CROSS JOIN store_calls
    WHERE events.tenant_id IS NOT NULL
),
ranked_sessions AS (
    SELECT
        event_sessions.*,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id ORDER BY created_at DESC
        ) AS tenant_rank
    FROM event_sessions
),
limited_sessions AS (
    SELECT
        tenant_id, session_id, task_type, model_name, local_cost_usd,
        cloud_cost_usd, counterfactual_baseline_usd, savings_usd,
        baseline_model, pricing_manifest_version, savings_method, usage_source,
        prompt_tokens, completion_tokens, tokens_to_compliance, latency_ms,
        created_at
    FROM ranked_sessions
    WHERE tenant_rank <= 500
),
totals AS (
    SELECT
        tenant_id,
        CAST(COALESCE(SUM(CASE WHEN usage_source = 'measured' THEN savings_usd END), 0) AS REAL) AS cumulative_savings_usd,
        CAST(COALESCE(SUM(CASE WHEN usage_source = 'measured' THEN local_cost_usd END), 0) AS REAL) AS cumulative_local_cost_usd,
        CAST(COALESCE(SUM(CASE WHEN usage_source = 'measured' THEN cloud_cost_usd END), 0) AS REAL) AS cumulative_cloud_cost_usd,
        CAST(COALESCE(SUM(CASE WHEN usage_source = 'measured' THEN counterfactual_baseline_usd END), 0) AS REAL)
            AS cumulative_counterfactual_baseline_usd,
        COUNT(*) AS session_count,
        SUM(CASE WHEN usage_source = 'measured' THEN 1 ELSE 0 END)
            AS measured_run_count,
        SUM(CASE WHEN usage_source = 'estimated' THEN 1 ELSE 0 END)
            AS estimated_run_count,
        SUM(CASE WHEN usage_source = 'unknown' THEN 1 ELSE 0 END)
            AS unknown_run_count,
        SUM(unpriced) AS unpriced_call_count,
        MAX(created_at) AS latest_projection_updated_at
    FROM event_sessions
    GROUP BY tenant_id
),
sessions AS (
    SELECT
        tenant_id,
        json_group_array(json_object(
            'session_id', session_id,
            'task_type', task_type,
            'model_name', model_name,
            'local_cost_usd', local_cost_usd,
            'cloud_cost_usd', cloud_cost_usd,
            'counterfactual_baseline_usd', counterfactual_baseline_usd,
            'savings_usd', savings_usd,
            'baseline_model', baseline_model,
            'pricing_manifest_version', pricing_manifest_version,
            'savings_method', savings_method,
            'usage_source', usage_source,
            'prompt_tokens', prompt_tokens,
            'completion_tokens', completion_tokens,
            'tokens_to_compliance', tokens_to_compliance,
            'latency_ms', latency_ms,
            'created_at', created_at
        )) AS rows
    FROM (
        SELECT * FROM limited_sessions ORDER BY created_at DESC
    ) ordered
    GROUP BY tenant_id
),
latest AS (
    -- The newest run that was priced, so an unpriced newest run never blanks
    -- the topic's baseline.
    SELECT tenant_id, baseline_model, pricing_manifest_version
    FROM (
        SELECT tenant_id, baseline_model, pricing_manifest_version,
            ROW_NUMBER() OVER (
                PARTITION BY tenant_id ORDER BY created_at DESC
            ) AS priced_rank
        FROM event_sessions
        WHERE baseline_model IS NOT NULL
    ) AS priced
    WHERE priced_rank = 1
)
SELECT
    totals.cumulative_savings_usd,
    totals.cumulative_local_cost_usd,
    totals.cumulative_cloud_cost_usd,
    latest.baseline_model AS baseline_model,
    COALESCE(latest.pricing_manifest_version, 'runtime-delegation-events')
        AS pricing_manifest_version,
    totals.session_count,
    COALESCE(sessions.rows, '[]') AS sessions,
    COALESCE(totals.latest_projection_updated_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        AS captured_at,
    1 AS provisioned,
    totals.latest_projection_updated_at,
    totals.cumulative_counterfactual_baseline_usd,
    totals.tenant_id,
    totals.unpriced_call_count,
    totals.measured_run_count,
    totals.estimated_run_count,
    totals.unknown_run_count
FROM totals
LEFT JOIN sessions ON sessions.tenant_id = totals.tenant_id
LEFT JOIN latest ON latest.tenant_id = totals.tenant_id;
