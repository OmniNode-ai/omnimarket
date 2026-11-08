-- OMN-21970: per-day delegation totals over the whole history, one row per
-- tenant.
--
-- The Overview's Runs per day and Tokens per day drew from the newest 500 rows
-- of the decisions exposure, so with more than 500 runs every older day showed
-- no runs and "not recorded" tokens. This view groups delegation_events by
-- tenant and UTC day and folds the days into one ordered "days" list per
-- tenant: one row per tenant is the singleton-aggregate shape the writer
-- republishes after every apply, keyed (snapshot_grain, tenant_id) like the
-- summary.
--
-- Rules, the same as the SQLite counterpart
-- (node_projection_delegation/sqlite/delegation_daily_view.sql):
--   * day is the UTC date of written_at, else created_at, as the dashboard
--     places a run;
--   * a run whose terminal was not ok failed whatever its quality gate said;
--     otherwise the quality gate decides; a run with neither is unrecorded;
--   * a token sum is NULL when no run that day carries the count, and the
--     *Unmeasured fields count the runs without it.

BEGIN;

CREATE OR REPLACE VIEW public.projection_delegation_daily AS
WITH per_day AS (
    SELECT
        tenant_id,
        (COALESCE(written_at, created_at) AT TIME ZONE 'UTC')::date AS day,
        COUNT(*)::int AS runs,
        COUNT(*) FILTER (WHERE COALESCE(terminal_ok, true)
            AND quality_gate_passed IS TRUE)::int AS passed,
        COUNT(*) FILTER (WHERE terminal_ok IS FALSE
            OR quality_gate_passed IS FALSE)::int AS failed,
        COUNT(*) FILTER (WHERE COALESCE(terminal_ok, true)
            AND quality_gate_passed IS NULL)::int AS unrecorded,
        SUM(tokens_input)::bigint AS tokens_input,
        SUM(tokens_output)::bigint AS tokens_output,
        COUNT(*) FILTER (WHERE tokens_input IS NULL)::int AS tokens_input_unmeasured,
        COUNT(*) FILTER (WHERE tokens_output IS NULL)::int AS tokens_output_unmeasured,
        MAX(COALESCE(written_at, created_at)) AS latest_written_at
    FROM public.delegation_events
    GROUP BY tenant_id, (COALESCE(written_at, created_at) AT TIME ZONE 'UTC')::date
)
SELECT
    tenant_id,
    jsonb_agg(
        jsonb_build_object(
            'day', to_char(day, 'YYYY-MM-DD'),
            'runs', runs,
            'passed', passed,
            'failed', failed,
            'unrecorded', unrecorded,
            'tokensIn', tokens_input,
            'tokensOut', tokens_output,
            'tokensInUnmeasured', tokens_input_unmeasured,
            'tokensOutUnmeasured', tokens_output_unmeasured
        )
        ORDER BY day
    ) AS days,
    SUM(runs)::int AS total_runs,
    MAX(latest_written_at) AS latest_projection_updated_at
FROM per_day
GROUP BY tenant_id;

-- security_invoker so the tenant policy on delegation_events applies to the
-- reader of this view, as 0040 set for the other aggregate views.
ALTER VIEW public.projection_delegation_daily SET (security_invoker = true);

GRANT SELECT ON public.projection_delegation_daily TO app_dashboard;
GRANT SELECT ON public.projection_delegation_daily TO tenant_projection_writer;

COMMIT;
