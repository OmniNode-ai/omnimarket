-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-24615 AC6 (the 2026-11-19 re-review of #6442): the revision of delegation_summary_view_v3.sql. One change,
-- nothing else: each "byModel" row also serves "backends", the sorted aliases its runs were reached through --
-- delegated_to where it is set and differs from the model that ran, as the metering record defines its backend --
-- so a page can tell that a model was reached through a backend alias. The view's columns are unchanged.
--
-- Applied as the store step omn24615_delegation_summary_backends after
-- omn24615_delegation_summary_evaluated_runs; a later revision is a new step.
CREATE VIEW projection_delegation_summary AS
WITH measured AS (
    SELECT tenant_id, quality_gate_passed,
        latency_ms, delegation_latency_ms, created_at, cost_savings_usd,
        data_source, task_type, delegated_to, quality_gates_checked,
        COALESCE(NULLIF(model_name, ''), delegated_to) AS served_model
    FROM delegation_events WHERE data_source IS NOT 'experiment'
),
summary AS (
    SELECT
        tenant_id,
        COUNT(*) AS total_events,
        COALESCE(SUM(CASE WHEN quality_gates_checked IS NOT 0 AND quality_gate_passed = 1 THEN 1 ELSE 0 END), 0)
            AS quality_passed_count,
        COALESCE(SUM(CASE WHEN quality_gates_checked IS NOT 0 AND quality_gate_passed = 0 THEN 1 ELSE 0 END), 0)
            AS quality_failed_count,
        COALESCE(SUM(CASE WHEN (quality_gates_checked IS NOT 0 AND quality_gate_passed IS NOT NULL) THEN 1 ELSE 0 END), 0)
            AS quality_checked_count,
        COALESCE(AVG(COALESCE(latency_ms, delegation_latency_ms)), 0.0)
            AS avg_latency_ms,
        COALESCE(MAX((julianday(created_at) - 2440587.5) * 86400.0), 0.0)
            AS latest_event_at,
        COALESCE(SUM(CASE WHEN data_source <> 'fixture' THEN cost_savings_usd END), 0.0)
            AS total_savings_usd,
        COALESCE(SUM(CASE WHEN data_source = 'fixture' THEN cost_savings_usd END), 0.0)
            AS fixture_savings_usd,
        COALESCE(SUM(CASE WHEN data_source = 'fixture' THEN 1 ELSE 0 END), 0)
            AS fixture_events,
        MAX(created_at) AS latest_projection_updated_at
    FROM measured
    GROUP BY tenant_id
),
by_task_type AS (
    SELECT tenant_id,
        json_group_array(json_object('taskType', task_type, 'count', count)) AS rows
    FROM (
        SELECT tenant_id, task_type, COUNT(*) AS count
        FROM measured
        GROUP BY tenant_id, task_type
        ORDER BY count DESC
    )
    GROUP BY tenant_id
),
model_backends AS (
    SELECT tenant_id, served_model AS served_model, json_group_array(backend) AS backends
    FROM (
        SELECT DISTINCT tenant_id, served_model AS served_model, delegated_to AS backend
        FROM measured
        WHERE COALESCE(delegated_to, '') <> ''
            AND delegated_to IS NOT served_model
        ORDER BY 1, 2, 3
    )
    GROUP BY tenant_id, served_model
),
by_model AS (
    SELECT tenant_id,
        json_group_array(json_object('model', served_model, 'count', count, 'backends', json(backends))) AS rows
    FROM (
        SELECT measured.tenant_id, measured.served_model, COUNT(*) AS count,
            COALESCE(MAX(model_backends.backends), '[]') AS backends
        FROM measured
        LEFT JOIN model_backends
            ON model_backends.tenant_id IS measured.tenant_id
            AND model_backends.served_model IS measured.served_model
        GROUP BY measured.tenant_id, measured.served_model
        ORDER BY count DESC
    )
    GROUP BY tenant_id
)
SELECT
    summary.tenant_id AS tenant_id,
    summary.total_events AS "totalDelegations",
    CASE WHEN summary.quality_checked_count > 0
        THEN CAST(summary.quality_passed_count AS REAL) / summary.quality_checked_count
        ELSE 0.0
    END AS "qualityGatePassRate",
    summary.quality_passed_count AS "qualityGatePassed",
    summary.quality_checked_count AS "qualityGateTotal",
    summary.total_savings_usd AS "totalSavingsUsd",
    summary.avg_latency_ms AS "avgLatencyMs",
    summary.latest_event_at AS "latestEventAt",
    summary.total_events AS total_events,
    summary.quality_passed_count AS quality_passed_count,
    summary.quality_failed_count AS quality_failed_count,
    summary.avg_latency_ms AS avg_latency_ms,
    summary.latest_event_at AS latest_event_at,
    COALESCE(by_task_type.rows, '[]') AS "byTaskType",
    COALESCE(by_model.rows, '[]') AS "byModel",
    summary.latest_projection_updated_at AS latest_projection_updated_at,
    summary.fixture_savings_usd AS "fixtureSavingsUsd",
    summary.fixture_events AS "fixtureDelegations"
FROM summary
LEFT JOIN by_task_type ON by_task_type.tenant_id IS summary.tenant_id
LEFT JOIN by_model ON by_model.tenant_id IS summary.tenant_id;
