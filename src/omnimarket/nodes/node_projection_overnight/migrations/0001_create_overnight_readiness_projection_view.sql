-- OMN-12839: dashboard readiness snapshot over overnight_sessions.
--
-- The readiness gate widget consumes a compact status summary rather than raw
-- overnight session rows. This view keeps the rollup server-side and exposes a
-- contract-owned projection topic through projection-api.

CREATE OR REPLACE VIEW projection_overnight_readiness AS
WITH totals AS (
    SELECT
        COUNT(*)::int AS session_count,
        COALESCE(SUM(CASE WHEN session_status = 'completed' THEN 1 ELSE 0 END), 0)::int
            AS completed_count,
        COALESCE(SUM(CASE WHEN session_status = 'partial' THEN 1 ELSE 0 END), 0)::int
            AS partial_count,
        COALESCE(SUM(CASE WHEN session_status = 'failed' THEN 1 ELSE 0 END), 0)::int
            AS failed_count,
        COALESCE(SUM(CASE WHEN session_status = 'in_progress' THEN 1 ELSE 0 END), 0)::int
            AS in_progress_count,
        COALESCE(SUM(dispatch_count), 0)::int AS dispatch_count,
        COALESCE(SUM(cardinality(phases_failed)), 0)::int AS failed_phase_count,
        MAX(updated_at) AS latest_projection_updated_at
    FROM overnight_sessions
),
status AS (
    SELECT
        CASE
            WHEN totals.session_count = 0 THEN 'WARN'
            WHEN totals.failed_count > 0 OR totals.failed_phase_count > 0 THEN 'FAIL'
            WHEN totals.partial_count > 0 OR totals.in_progress_count > 0 THEN 'WARN'
            ELSE 'PASS'
        END AS overall_status
    FROM totals
),
dimensions AS (
    SELECT jsonb_build_array(
        jsonb_build_object(
            'name', 'Sessions completed',
            'status', CASE
                WHEN totals.session_count = 0 THEN 'WARN'
                WHEN totals.completed_count > 0 THEN 'PASS'
                ELSE 'WARN'
            END,
            'detail', totals.completed_count::text || ' completed of '
                || totals.session_count::text || ' tracked sessions'
        ),
        jsonb_build_object(
            'name', 'Failed phases',
            'status', CASE
                WHEN totals.failed_count > 0 OR totals.failed_phase_count > 0 THEN 'FAIL'
                ELSE 'PASS'
            END,
            'detail', totals.failed_phase_count::text || ' failed phases across '
                || totals.failed_count::text || ' failed sessions'
        ),
        jsonb_build_object(
            'name', 'Dispatch activity',
            'status', CASE
                WHEN totals.session_count = 0 THEN 'WARN'
                WHEN totals.dispatch_count > 0 THEN 'PASS'
                ELSE 'WARN'
            END,
            'detail', totals.dispatch_count::text || ' dispatches recorded'
        )
    ) AS rows
    FROM totals
)
SELECT
    dimensions.rows AS dimensions,
    status.overall_status AS "overallStatus",
    COALESCE(totals.latest_projection_updated_at, NOW()) AS "lastCheckedAt",
    totals.latest_projection_updated_at
FROM totals
CROSS JOIN status
CROSS JOIN dimensions;
