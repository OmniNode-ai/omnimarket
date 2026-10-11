-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-20008: the SQLite counterpart of node_projection_savings migration
-- 090_savings_aggregate_excludes_model_text.sql's projection_delegation_savings
-- view, so a local store serves onex.snapshot.projection.delegation.savings.v1
-- with each session's basis read from its stored call provenance.
--
-- Same columns, names, JSON keys and aggregates as migration 090, with these
-- deliberate differences from Postgres:
-- * A local store has no savings_estimates table, so sessions come only from
--   delegation_events: 090's event_sessions expressions and tenant_id IS NOT
--   NULL filter, with baseline_model NULL for every session.
-- * A local store has no cost_measurement_source column. usage_source comes
--   from llm_call_metrics joined on its correlation_id = delegation_events'
--   correlation_id: measured requires at least one call and every call to be
--   measured; no calls, any unknown or any value outside measured/estimated
--   gives unknown; otherwise it is estimated. Token counts never infer it.
--   savings_method is measured only when usage_source is measured, else
--   estimated.
-- * Each session's pricing_manifest_version is CAST AS TEXT because the
--   local store writes it as an integer.
-- * sessions uses json_group_array(json_object(...)) over the newest 500
--   sessions per tenant, newest first, from an ordered subquery: SQLite 3.40
--   has no ORDER BY inside an aggregate. Element keys are exactly 090's
--   limited_sessions columns minus tenant_id, excluding full model text.
--   Cost fields are CAST(COALESCE(x, 0) AS REAL); token fields are INTEGER.
-- * provisioned is 1 instead of TRUE; captured_at is
--   COALESCE(latest_projection_updated_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
--   instead of using Postgres NOW().
-- * Top-level pricing_manifest_version is COALESCE(the latest session's,
--   'runtime-delegation-events'); baseline_model is the latest session's
--   (NULL locally). Totals cover all sessions, grouped by tenant_id.
--
-- Applied once per local store by SqliteDatabaseAdapter as the store step
-- omn20008_delegation_savings_view; a revision is a new step, never an edit here.
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
event_sessions AS (
    SELECT
        events.tenant_id AS tenant_id,
        COALESCE(NULLIF(events.correlation_id, ''), NULLIF(events.session_id, ''),
            CAST(events.id AS TEXT)) AS session_id,
        COALESCE(events.task_type, '') AS task_type,
        COALESCE(NULLIF(events.model_name, ''), NULLIF(events.delegated_to, ''), 'local')
            AS model_name,
        CAST(COALESCE(events.cost_usd, 0) AS REAL) AS local_cost_usd,
        CAST(COALESCE(events.cost_usd, 0) + COALESCE(events.cost_savings_usd, 0) AS REAL)
            AS cloud_cost_usd,
        CAST(COALESCE(events.cost_usd, 0) + COALESCE(events.cost_savings_usd, 0) AS REAL)
            AS counterfactual_baseline_usd,
        CAST(COALESCE(events.cost_savings_usd, 0) AS REAL) AS savings_usd,
        NULL AS baseline_model,
        CAST(events.pricing_manifest_version AS TEXT) AS pricing_manifest_version,
        CASE WHEN call_usage.usage_source = 'measured'
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        COALESCE(call_usage.usage_source, 'unknown') AS usage_source,
        CAST(COALESCE(events.tokens_input, 0) AS INTEGER) AS prompt_tokens,
        CAST(COALESCE(events.tokens_output, 0) AS INTEGER) AS completion_tokens,
        CAST(NULLIF(events.tokens_to_compliance, 0) AS INTEGER) AS tokens_to_compliance,
        CAST(COALESCE(events.delegation_latency_ms, events.latency_ms) AS INTEGER)
            AS latency_ms,
        COALESCE(events.created_at, events.timestamp) AS created_at
    FROM delegation_events AS events
    LEFT JOIN call_usage ON call_usage.correlation_id = events.correlation_id
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
        CAST(COALESCE(SUM(savings_usd), 0) AS REAL) AS cumulative_savings_usd,
        CAST(COALESCE(SUM(local_cost_usd), 0) AS REAL) AS cumulative_local_cost_usd,
        CAST(COALESCE(SUM(cloud_cost_usd), 0) AS REAL) AS cumulative_cloud_cost_usd,
        CAST(COALESCE(SUM(counterfactual_baseline_usd), 0) AS REAL)
            AS cumulative_counterfactual_baseline_usd,
        COUNT(*) AS session_count,
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
    SELECT tenant_id, baseline_model, pricing_manifest_version
    FROM ranked_sessions
    WHERE tenant_rank = 1
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
    totals.tenant_id
FROM totals
LEFT JOIN sessions ON sessions.tenant_id = totals.tenant_id
LEFT JOIN latest ON latest.tenant_id = totals.tenant_id;
