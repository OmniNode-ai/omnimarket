-- OMN-26623: weekly delegation quality, one row per tenant.
-- cells holds every ISO week, task type and first-rung model with runs,
-- first-try passes and moves down the chain.
-- Definitions Jonah confirmed on 2026-11-26: first-try pass; moved down the
-- chain, where a later rung on the same model is a redraw; escalation_count
-- is not read.
-- A week with no runs serves runs 0 and NULL rates, never 0.
-- Same rules as the SQLite counterpart
-- node_projection_delegation/sqlite/delegation_quality_weekly_view.sql.
-- NULLS FIRST and COLLATE "C" keep the cells in the SQLite order.
-- A tenant with any delegation_events row is served a row; one whose rows carry no attempt history
-- is served empty cells and total_runs 0.
-- Vendored into omnibase_infra byte for byte.

BEGIN;

CREATE OR REPLACE VIEW public.projection_delegation_quality_weekly AS
WITH eligible AS (
    SELECT
        tenant_id,
        created_at,
        task_type,
        date_trunc('week', created_at AT TIME ZONE 'UTC')::date AS week_start,
        attempt_history -> 0 ->> 'model_id' AS model_id,
        CASE
            WHEN (attempt_history -> 0 ->> 'attempt_kind') = 'first_try'
             AND (attempt_history -> 0 -> 'quality_gate_passed') = 'true'::jsonb
             AND COALESCE(attempt_history -> 0 -> 'failure_class', 'null'::jsonb) = 'null'::jsonb
             AND terminal_ok IS TRUE
            THEN 1 ELSE 0
        END AS first_try_pass,
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM jsonb_array_elements(attempt_history) WITH ORDINALITY AS rung(value, position)
                WHERE rung.position > 1
                  AND (rung.value ->> 'model_id') IS DISTINCT FROM (attempt_history -> 0 ->> 'model_id')
            )
            THEN 1 ELSE 0
        END AS moved
    FROM public.delegation_events
    WHERE data_source IS DISTINCT FROM 'experiment'
      AND tenant_id IS NOT NULL
      AND jsonb_typeof(attempt_history) = 'array'
      AND jsonb_array_length(attempt_history) > 0
),
counted AS (
    SELECT
        tenant_id,
        week_start,
        task_type,
        model_id,
        COUNT(*)::int AS runs,
        SUM(first_try_pass)::int AS first_try_passes,
        SUM(moved)::int AS moved_down_chain
    FROM eligible
    GROUP BY tenant_id, week_start, task_type, model_id
),
bounds AS (
    SELECT
        tenant_id,
        MIN(week_start) AS first_week,
        MAX(week_start) AS last_week,
        MAX(created_at) AS latest_projection_updated_at
    FROM eligible
    GROUP BY tenant_id
),
tenants AS (
    SELECT tenant_id, MAX(created_at) AS latest_projection_updated_at
    FROM public.delegation_events
    WHERE tenant_id IS NOT NULL
    GROUP BY tenant_id
),
weeks AS (
    SELECT tenant_id, generate_series(first_week, last_week, interval '7 days')::date AS week_start
    FROM bounds
),
pairs AS (
    SELECT DISTINCT tenant_id, task_type, model_id
    FROM counted
),
cells AS (
    SELECT
        weeks.tenant_id AS tenant_id,
        weeks.week_start AS week_start,
        pairs.task_type AS task_type,
        pairs.model_id AS model_id
    FROM weeks
    JOIN pairs ON pairs.tenant_id = weeks.tenant_id
),
cell_rows AS (
    SELECT
        cells.tenant_id AS tenant_id,
        to_char(cells.week_start, 'IYYY-"W"IW') AS iso_week,
        cells.week_start AS week_start,
        to_char(cells.week_start, 'YYYY-MM-DD') AS week_start_text,
        cells.task_type AS task_type,
        cells.model_id AS model_id,
        COALESCE(counted.runs, 0) AS runs,
        COALESCE(counted.first_try_passes, 0) AS first_try_passes,
        COALESCE(counted.moved_down_chain, 0) AS moved_down_chain,
        CASE WHEN COALESCE(counted.runs, 0) > 0 THEN counted.first_try_passes::float8 / counted.runs END AS first_try_pass_rate,
        CASE WHEN COALESCE(counted.runs, 0) > 0 THEN counted.moved_down_chain::float8 / counted.runs END AS moved_down_chain_rate,
        bounds.latest_projection_updated_at AS latest_projection_updated_at
    FROM cells
    LEFT JOIN counted
      ON counted.tenant_id = cells.tenant_id
     AND counted.week_start = cells.week_start
     AND counted.task_type IS NOT DISTINCT FROM cells.task_type
     AND counted.model_id IS NOT DISTINCT FROM cells.model_id
    JOIN bounds ON bounds.tenant_id = cells.tenant_id
)
SELECT
    tenants.tenant_id AS tenant_id,
    COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'iso_week', cell_rows.iso_week,
                'week_start', cell_rows.week_start_text,
                'task_type', cell_rows.task_type,
                'model_id', cell_rows.model_id,
                'runs', cell_rows.runs,
                'first_try_passes', cell_rows.first_try_passes,
                'moved_down_chain', cell_rows.moved_down_chain,
                'first_try_pass_rate', cell_rows.first_try_pass_rate,
                'moved_down_chain_rate', cell_rows.moved_down_chain_rate
            )
            ORDER BY cell_rows.week_start DESC, cell_rows.task_type COLLATE "C" NULLS FIRST, cell_rows.model_id COLLATE "C" NULLS FIRST
        ) FILTER (WHERE cell_rows.iso_week IS NOT NULL),
        '[]'::jsonb
    ) AS cells,
    COALESCE(SUM(cell_rows.runs), 0)::int AS total_runs,
    COALESCE(MAX(cell_rows.latest_projection_updated_at), tenants.latest_projection_updated_at) AS latest_projection_updated_at
FROM tenants
LEFT JOIN cell_rows ON cell_rows.tenant_id = tenants.tenant_id
GROUP BY tenants.tenant_id;

ALTER VIEW public.projection_delegation_quality_weekly SET (security_invoker = true);

GRANT SELECT ON public.projection_delegation_quality_weekly TO app_dashboard;
GRANT SELECT ON public.projection_delegation_quality_weekly TO tenant_projection_writer;

COMMIT;
