-- OMN-19970: the delegation summary's measured savings exclude fixture rows.
--
-- Migration 0049 labels every delegation_events row 'real' or 'fixture'. The
-- Overview's savings headline is a measured figure, so "totalSavingsUsd" now
-- sums 'real' rows only. The fixture figures are reported beside it as
-- "fixtureSavingsUsd" and "fixtureDelegations", so a page can show seeded rows,
-- label them, and offer a visible toggle to include them -- without a fixture
-- ever entering the measured number. Counts and quality rates are unchanged.
--
-- The body is 0045's projection_delegation_summary with two edits: the savings
-- SUM gains FILTER (WHERE data_source <> 'fixture'), and two columns are
-- appended at the end (CREATE OR REPLACE VIEW may only append).
--
-- security_invoker: CREATE OR REPLACE VIEW drops it (0045's own note), so it
-- is set again below, or the view would stop inheriting delegation_events'
-- row-level security.

BEGIN;

CREATE OR REPLACE VIEW projection_delegation_summary AS
WITH summary AS (
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
    FROM delegation_events
    GROUP BY tenant_id
),
by_task_type AS (
    SELECT tenant_id, COALESCE(
        jsonb_agg(jsonb_build_object('taskType', task_type, 'count', count)
            ORDER BY count DESC), '[]'::jsonb
    ) AS rows
    FROM (
        SELECT tenant_id, task_type, COUNT(*)::int AS count
        FROM delegation_events
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
        FROM delegation_events
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
    -- OMN-19970: appended, never reordered -- CREATE OR REPLACE VIEW may only add
    -- columns at the end.
    summary.fixture_savings_usd AS "fixtureSavingsUsd",
    summary.fixture_events AS "fixtureDelegations"
FROM summary
LEFT JOIN by_task_type USING (tenant_id)
LEFT JOIN by_model USING (tenant_id);

ALTER VIEW projection_delegation_summary SET (security_invoker = true);

COMMIT;
