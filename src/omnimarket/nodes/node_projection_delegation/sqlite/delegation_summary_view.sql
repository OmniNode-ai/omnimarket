-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-20709: the SQLite counterpart of node_projection_delegation migration
-- 0050's projection_delegation_summary view. On Postgres the summary is a view
-- over delegation_events; a local store had no such relation, so the dashboard
-- listed the exposure and every read of it answered 503 projection_table_missing.
-- Same columns, names and aggregates as migration 0050, with three deliberate
-- differences, each because the local store cannot express the Postgres form:
-- * The quality counts skip a row whose quality_gate_passed is NULL. Postgres
--   additionally drops the ('terminal_construction_failed', 'undetermined')
--   outcome pair, but operational_outcome and content_verdict are not columns
--   a local store has, so no local row can carry that pair.
-- * latestEventAt is the epoch of created_at, computed through julianday
--   because SQLite has no EXTRACT(EPOCH ...).
-- * byTaskType and byModel are json_group_array text; the exposure declares
--   both as json_columns, so the read decodes them to the same lists jsonb_agg
--   returns.
--
-- Applied once per local store by SqliteDatabaseAdapter as the store step
-- omn20709_delegation_summary_view; a revision is a new step, never an edit here.
CREATE VIEW projection_delegation_summary AS
WITH summary AS (
    SELECT
        tenant_id,
        COUNT(*) AS total_events,
        COALESCE(SUM(CASE WHEN quality_gate_passed = 1 THEN 1 ELSE 0 END), 0)
            AS quality_passed_count,
        COALESCE(SUM(CASE WHEN quality_gate_passed = 0 THEN 1 ELSE 0 END), 0)
            AS quality_failed_count,
        COALESCE(SUM(CASE WHEN quality_gate_passed IS NOT NULL THEN 1 ELSE 0 END), 0)
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
    FROM delegation_events
    GROUP BY tenant_id
),
by_task_type AS (
    SELECT tenant_id,
        json_group_array(json_object('taskType', task_type, 'count', count)) AS rows
    FROM (
        SELECT tenant_id, task_type, COUNT(*) AS count
        FROM delegation_events
        GROUP BY tenant_id, task_type
        ORDER BY count DESC
    )
    GROUP BY tenant_id
),
by_model AS (
    SELECT tenant_id,
        json_group_array(json_object('model', delegated_to, 'count', count)) AS rows
    FROM (
        SELECT tenant_id, delegated_to, COUNT(*) AS count
        FROM delegation_events
        GROUP BY tenant_id, delegated_to
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
