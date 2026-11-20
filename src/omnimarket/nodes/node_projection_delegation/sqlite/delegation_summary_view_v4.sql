-- OMN-23413 AC51: the revision after delegation_summary_view_v3.sql (OMN-24615). One change, nothing else:
-- the headline savings keep a run whose data_source is NULL. v3 compared with `<>`, and a NULL compared with
-- `<>` is never true, so runs written before data_source was classified dropped out of the savings while v3
-- already counts them everywhere else. `IS NOT 'fixture'` drops only runs typed fixture, as v2 intended.
--
-- Applied as the store step omn23413_delegation_summary_counts_unclassified after
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
        COALESCE(SUM(CASE WHEN data_source IS NOT 'fixture' THEN cost_savings_usd END), 0.0)
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
by_model AS (
    SELECT tenant_id,
        json_group_array(json_object('model', served_model, 'count', count)) AS rows
    FROM (
        SELECT tenant_id, served_model, COUNT(*) AS count
        FROM measured
        GROUP BY tenant_id, served_model
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
