-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- The view folds delegation_events into one row per tenant, ISO week, task type and first-rung model.
-- The two figures follow the definitions Jonah confirmed on 2026-11-26: first-try pass; moved down the chain, where a later rung on the same model is a redraw, and escalation_count is not read.
-- A week with no runs for a task type and model is served with runs 0 and NULL rates, never 0.
-- The ISO week is computed from the week's Thursday because SQLite before 3.46 has no %G/%V.
-- Applied once per local store by SqliteDatabaseAdapter as the store step omn26623_delegation_quality_weekly_view, and a revision is a new step, never an edit here.
-- The view serves one row per tenant with any delegation_events row; its cells list holds every week, task type and model, newest week first.
-- A tenant whose rows carry no attempt history is served a row with empty cells and total_runs 0.
-- A NULL rate stays JSON null inside cells.

CREATE VIEW projection_delegation_quality_weekly AS
WITH RECURSIVE
eligible AS (
    SELECT
        tenant_id,
        created_at,
        task_type,
        date(created_at, '-' || ((CAST(strftime('%w', created_at) AS INTEGER) + 6) % 7) || ' days') AS week_start,
        json_extract(attempt_history, '$[0].model_id') AS model_id,
        CASE
            WHEN json_extract(attempt_history, '$[0].attempt_kind') = 'first_try'
             AND json_extract(attempt_history, '$[0].quality_gate_passed') = 1
             AND json_extract(attempt_history, '$[0].failure_class') IS NULL
             AND terminal_ok = 1
            THEN 1 ELSE 0
        END AS first_try_pass,
        CASE
            WHEN EXISTS (
                SELECT 1
                FROM json_each(attempt_history) AS rung
                WHERE rung.key > 0
                  AND json_extract(rung.value, '$.model_id') IS NOT json_extract(attempt_history, '$[0].model_id')
            )
            THEN 1 ELSE 0
        END AS moved
    FROM delegation_events
    WHERE data_source IS NOT 'experiment'
      AND tenant_id IS NOT NULL
      AND json_valid(attempt_history)
      AND json_array_length(attempt_history) > 0
),
counted AS (
    SELECT
        tenant_id,
        week_start,
        task_type,
        model_id,
        COUNT(*) AS runs,
        SUM(first_try_pass) AS first_try_passes,
        SUM(moved) AS moved_down_chain
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
    FROM delegation_events
    WHERE tenant_id IS NOT NULL
    GROUP BY tenant_id
),
weeks AS (
    SELECT tenant_id, first_week AS week_start
    FROM bounds
    UNION ALL
    SELECT w.tenant_id, date(w.week_start, '+7 days')
    FROM weeks AS w
    JOIN bounds AS b ON b.tenant_id = w.tenant_id
    WHERE w.week_start < b.last_week
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
        strftime('%Y', date(cells.week_start, '+3 days')) || '-W' || printf('%02d', (CAST(strftime('%j', date(cells.week_start, '+3 days')) AS INTEGER) - 1) / 7 + 1) AS iso_week,
        cells.week_start AS week_start,
        cells.task_type AS task_type,
        cells.model_id AS model_id,
        COALESCE(counted.runs, 0) AS runs,
        COALESCE(counted.first_try_passes, 0) AS first_try_passes,
        COALESCE(counted.moved_down_chain, 0) AS moved_down_chain,
        CASE WHEN COALESCE(counted.runs, 0) > 0 THEN CAST(counted.first_try_passes AS REAL) / counted.runs ELSE NULL END AS first_try_pass_rate,
        CASE WHEN COALESCE(counted.runs, 0) > 0 THEN CAST(counted.moved_down_chain AS REAL) / counted.runs ELSE NULL END AS moved_down_chain_rate,
        bounds.latest_projection_updated_at AS latest_projection_updated_at
    FROM cells
    LEFT JOIN counted
      ON counted.tenant_id = cells.tenant_id
     AND counted.week_start = cells.week_start
     AND counted.task_type IS cells.task_type
     AND counted.model_id IS cells.model_id
    JOIN bounds ON bounds.tenant_id = cells.tenant_id
)
SELECT
    tenants.tenant_id AS tenant_id,
    json_group_array(json_object('iso_week', cell_rows.iso_week, 'week_start', cell_rows.week_start, 'task_type', cell_rows.task_type, 'model_id', cell_rows.model_id, 'runs', cell_rows.runs, 'first_try_passes', cell_rows.first_try_passes, 'moved_down_chain', cell_rows.moved_down_chain, 'first_try_pass_rate', cell_rows.first_try_pass_rate, 'moved_down_chain_rate', cell_rows.moved_down_chain_rate)) FILTER (WHERE cell_rows.iso_week IS NOT NULL) AS cells,
    COALESCE(SUM(cell_rows.runs), 0) AS total_runs,
    COALESCE(MAX(cell_rows.latest_projection_updated_at), tenants.latest_projection_updated_at) AS latest_projection_updated_at
FROM tenants
LEFT JOIN (SELECT * FROM cell_rows ORDER BY tenant_id, week_start DESC, task_type, model_id) AS cell_rows
  ON cell_rows.tenant_id = tenants.tenant_id
GROUP BY tenants.tenant_id;
