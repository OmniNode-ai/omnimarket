-- OMN-23169: the delegation summary leaves typed synthetic (experiment) rows out.
--
-- Migration 0064 stores data_source 'experiment' for a terminal whose
-- provenance traffic class is synthetic, but 0050's summary still counted those
-- rows in the run count, the quality rates, the by-task-type and by-model lists
-- and the savings sum. A user's totals therefore rose with every experiment
-- run. The rows stay in delegation_events, so the Runs listing (which reads the
-- table, not this view) still shows them.
--
-- The body is 0050's projection_delegation_summary with one edit: every
-- aggregate reads from `measured`, delegation_events without experiment rows.
-- The column list is unchanged. `fixture` rows keep their 0050 treatment (counted,
-- their savings reported apart). security_invoker is set again below because
-- CREATE OR REPLACE VIEW drops it; `measured` is a CTE over delegation_events, so
-- the view still inherits that table's row-level security. `measured` names its
-- columns instead of reading `*`, so the view depends only on the columns it
-- reads and a later migration can still drop or alter any other delegation_events
-- column (a SELECT * would pin every column, as migration 0059's repeat test shows).

BEGIN;

CREATE OR REPLACE VIEW projection_delegation_summary AS
WITH measured AS (
    SELECT tenant_id, quality_gate_passed, operational_outcome, content_verdict,
        latency_ms, delegation_latency_ms, created_at, cost_savings_usd,
        data_source, task_type, delegated_to
    FROM delegation_events WHERE data_source <> 'experiment'
),
summary AS (
    SELECT
        tenant_id,
        COUNT(*)::int AS total_events,
        COALESCE(SUM(CASE WHEN quality_gate_passed
            AND (operational_outcome, content_verdict) IS DISTINCT FROM
                ('terminal_construction_failed', 'undetermined')
            THEN 1 ELSE 0 END), 0)::int AS quality_passed_count,
        COALESCE(SUM(CASE WHEN NOT quality_gate_passed
            AND (operational_outcome, content_verdict) IS DISTINCT FROM
                ('terminal_construction_failed', 'undetermined')
            THEN 1 ELSE 0 END), 0)::int AS quality_failed_count,
        COUNT(*) FILTER (WHERE quality_gate_passed IS NOT NULL
            AND (operational_outcome, content_verdict) IS DISTINCT FROM
                ('terminal_construction_failed', 'undetermined'))::int AS quality_checked_count,
        COALESCE(AVG(COALESCE(latency_ms, delegation_latency_ms)), 0)::float
            AS avg_latency_ms,
        COALESCE(MAX(EXTRACT(EPOCH FROM (created_at))), 0)::float AS latest_event_at,
        COALESCE(SUM(cost_savings_usd) FILTER (WHERE data_source <> 'fixture'), 0)::float
            AS total_savings_usd,
        COALESCE(SUM(cost_savings_usd) FILTER (WHERE data_source = 'fixture'), 0)::float
            AS fixture_savings_usd,
        COUNT(*) FILTER (WHERE data_source = 'fixture')::int AS fixture_events,
        MAX(created_at) AS latest_projection_updated_at
    FROM measured
    GROUP BY tenant_id
),
by_task_type AS (
    SELECT tenant_id, COALESCE(
        jsonb_agg(jsonb_build_object('taskType', task_type, 'count', count)
            ORDER BY count DESC), '[]'::jsonb
    ) AS rows
    FROM (
        SELECT tenant_id, task_type, COUNT(*)::int AS count
        FROM measured
        GROUP BY tenant_id, task_type
    ) grouped
    GROUP BY tenant_id
),
by_model AS (
    SELECT tenant_id, COALESCE(
        jsonb_agg(jsonb_build_object('model', delegated_to, 'count', count)
            ORDER BY count DESC), '[]'::jsonb
    ) AS rows
    FROM (
        SELECT tenant_id, delegated_to, COUNT(*)::int AS count
        FROM measured
        GROUP BY tenant_id, delegated_to
    ) grouped
    GROUP BY tenant_id
)
SELECT
    summary.tenant_id,
    summary.total_events AS "totalDelegations",
    CASE WHEN summary.quality_checked_count > 0
        THEN summary.quality_passed_count::float / summary.quality_checked_count
        ELSE 0
    END AS "qualityGatePassRate",
    summary.quality_passed_count AS "qualityGatePassed",
    summary.quality_checked_count AS "qualityGateTotal",
    summary.total_savings_usd AS "totalSavingsUsd",
    summary.avg_latency_ms AS "avgLatencyMs",
    summary.latest_event_at AS "latestEventAt",
    summary.total_events,
    summary.quality_passed_count,
    summary.quality_failed_count,
    summary.avg_latency_ms,
    summary.latest_event_at,
    COALESCE(by_task_type.rows, '[]'::jsonb) AS "byTaskType",
    COALESCE(by_model.rows, '[]'::jsonb) AS "byModel",
    summary.latest_projection_updated_at,
    summary.fixture_savings_usd AS "fixtureSavingsUsd",
    summary.fixture_events AS "fixtureDelegations"
FROM summary
LEFT JOIN by_task_type USING (tenant_id)
LEFT JOIN by_model USING (tenant_id);

ALTER VIEW projection_delegation_summary SET (security_invoker = true);

COMMIT;
