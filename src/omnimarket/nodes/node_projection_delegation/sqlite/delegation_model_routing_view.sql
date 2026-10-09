-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-20754: the SQLite counterpart of migration 0055's
-- projection_delegation_model_routing view, so a local store serves the
-- model-routing exposure the Overview's Run locally and Tier mix rows read.
--
-- Same columns, names, JSON keys and aggregates as migration 0055, with these
-- differences, each because the local store cannot express the Postgres form:
-- * The quality counts skip a row whose quality_gate_passed is NULL. Postgres
--   also drops the ('terminal_construction_failed', 'undetermined') outcome
--   pair, but a local store has no operational_outcome or content_verdict
--   column, so no local row can carry that pair.
-- * cost_tier_name is NOT NULL DEFAULT '' on Postgres (migration 0018); a local
--   row that never carried it holds NULL, so it is read as ''.
-- * Booleans are written as JSON true/false explicitly: SQLite stores them as
--   0/1, and json_object would otherwise emit numbers where jsonb has booleans.
-- * top_task_type breaks a count tie by task_type, where array_agg leaves the
--   order of equal counts unspecified.
-- * Epoch seconds come from julianday, rounded to the millisecond its float
--   arithmetic can only approximate (SQLite has no EXTRACT(EPOCH ...)), and
--   ordered JSON arrays are built from ordered subqueries, because SQLite 3.40
--   (Debian bookworm's) has no ORDER BY inside an aggregate.
-- * provisioned is 1 where Postgres has TRUE: a SQLite column has no boolean.
--
-- Applied once per local store by SqliteDatabaseAdapter as the store step
-- omn20754_delegation_routing_quality_views; a revision is a new step, never an
-- edit here.
CREATE VIEW projection_delegation_model_routing AS
WITH events AS (
    SELECT
        *,
        COALESCE(cost_tier_name, '') AS tier_name,
        COALESCE(NULLIF(model_name, ''), delegated_to) AS resolved_model
    FROM delegation_events
),
grouped AS (
    SELECT
        tenant_id,
        delegated_to AS model_alias,
        resolved_model AS model_name,
        task_type,
        COUNT(*) AS event_count,
        COALESCE(SUM(CASE WHEN quality_gate_passed = 1 THEN 1 ELSE 0 END), 0)
            AS quality_passed,
        COALESCE(SUM(CASE WHEN quality_gate_passed IS NOT NULL THEN 1 ELSE 0 END), 0)
            AS quality_checks,
        COALESCE(AVG(COALESCE(latency_ms, delegation_latency_ms)), 0.0) AS avg_latency_ms
    FROM events
    GROUP BY tenant_id, delegated_to, resolved_model, task_type
),
totals AS (
    SELECT
        tenant_id,
        COUNT(*) AS total_delegations,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
    GROUP BY tenant_id
),
model_totals AS (
    SELECT
        tenant_id,
        model_alias,
        SUM(event_count) AS total_count,
        SUM(quality_passed) AS quality_passed,
        SUM(quality_checks) AS quality_checks,
        CASE WHEN SUM(event_count) > 0
            THEN SUM(avg_latency_ms * event_count) / SUM(event_count)
            ELSE 0.0
        END AS avg_latency_ms
    FROM grouped
    GROUP BY tenant_id, model_alias
),
model_task_types AS (
    SELECT
        tenant_id,
        model_alias,
        json_group_array(task_type) AS task_types
    FROM (
        SELECT tenant_id, model_alias, task_type
        FROM grouped
        ORDER BY tenant_id, model_alias, task_type
    )
    GROUP BY tenant_id, model_alias
),
model_top_task AS (
    SELECT tenant_id, model_alias, task_type AS top_task_type
    FROM (
        SELECT
            tenant_id,
            model_alias,
            task_type,
            row_number() OVER (
                PARTITION BY tenant_id, model_alias
                ORDER BY event_count DESC, task_type
            ) AS rank
        FROM grouped
    )
    WHERE rank = 1
),
by_model AS (
    SELECT tenant_id, json_group_array(json(entry)) AS rows
    FROM (
        SELECT
            model_totals.tenant_id,
            json_object(
                'model_name', model_totals.model_alias,
                'total_count', model_totals.total_count,
                'pct_of_total', CASE WHEN totals.total_delegations > 0
                    THEN CAST(model_totals.total_count AS REAL) / totals.total_delegations
                    ELSE 0.0 END,
                'top_task_type', model_top_task.top_task_type,
                'avg_latency_ms', model_totals.avg_latency_ms,
                'qg_pass_rate', CASE WHEN model_totals.quality_checks > 0
                    THEN CAST(model_totals.quality_passed AS REAL) / model_totals.quality_checks
                    ELSE 0.0 END,
                'task_types', json(model_task_types.task_types)
            ) AS entry
        FROM model_totals
        JOIN totals ON totals.tenant_id IS model_totals.tenant_id
        JOIN model_task_types
            ON model_task_types.tenant_id IS model_totals.tenant_id
            AND model_task_types.model_alias IS model_totals.model_alias
        JOIN model_top_task
            ON model_top_task.tenant_id IS model_totals.tenant_id
            AND model_top_task.model_alias IS model_totals.model_alias
        ORDER BY model_totals.tenant_id, model_totals.total_count DESC
    )
    GROUP BY tenant_id
),
routing_rows AS (
    SELECT tenant_id, json_group_array(json(entry)) AS rows
    FROM (
        SELECT
            grouped.tenant_id,
            json_object(
                'model_name', grouped.model_alias,
                'task_type', grouped.task_type,
                'count', grouped.event_count,
                'pct_of_model', CASE WHEN model_totals.total_count > 0
                    THEN CAST(grouped.event_count AS REAL) / model_totals.total_count
                    ELSE 0.0 END,
                'pct_of_total', CASE WHEN totals.total_delegations > 0
                    THEN CAST(grouped.event_count AS REAL) / totals.total_delegations
                    ELSE 0.0 END
            ) AS entry
        FROM grouped
        JOIN model_totals
            ON model_totals.tenant_id IS grouped.tenant_id
            AND model_totals.model_alias IS grouped.model_alias
        JOIN totals ON totals.tenant_id IS grouped.tenant_id
        ORDER BY grouped.tenant_id, grouped.event_count DESC
    )
    GROUP BY tenant_id
),
decision_traces AS (
    SELECT tenant_id, json_group_array(json(entry)) AS rows
    FROM (
        SELECT
            tenant_id,
            created_at,
            json_object(
                'id', id,
                'correlation_id', correlation_id,
                'task_type', task_type,
                'model_name', resolved_model,
                'delegated_to', delegated_to,
                'routing_rule', NULL,
                'routing_confidence', NULL,
                'routing_candidates', NULL,
                'latency_ms', COALESCE(latency_ms, delegation_latency_ms),
                'quality_gate_passed', CASE quality_gate_passed
                    WHEN 1 THEN json('true')
                    WHEN 0 THEN json('false')
                END,
                'created_at', round((julianday(created_at) - 2440587.5) * 86400.0, 3)
            ) AS entry
        FROM (
            SELECT
                events.*,
                row_number() OVER (
                    PARTITION BY tenant_id ORDER BY created_at DESC
                ) AS id
            FROM events
        )
        WHERE id <= 20
        ORDER BY tenant_id, created_at DESC
    )
    GROUP BY tenant_id
),
tier_totals AS (
    SELECT
        tenant_id,
        COUNT(*) AS total_tasks,
        COALESCE(SUM(CASE WHEN tier_name <> '' THEN 1 ELSE 0 END), 0) AS tier_routed_total,
        COALESCE(SUM(CASE WHEN tier_name = '' THEN 1 ELSE 0 END), 0) AS not_tier_routed_count,
        COALESCE(SUM(CASE WHEN tier_name = 'local' THEN 1 ELSE 0 END), 0) AS local_call_count
    FROM events
    GROUP BY tenant_id
),
tier_rows AS (
    SELECT
        tenant_id,
        CASE WHEN tier_name = '' THEN 'not_tier_routed' ELSE tier_name END AS cost_tier_name,
        CASE WHEN tier_name <> '' THEN 1 ELSE 0 END AS tier_routed,
        COUNT(*) AS count
    FROM events
    GROUP BY tenant_id, 2, 3
),
tier_list AS (
    SELECT tenant_id, json_group_array(json(entry)) AS tiers
    FROM (
        SELECT
            tier_rows.tenant_id,
            json_object(
                'cost_tier_name', tier_rows.cost_tier_name,
                'count', tier_rows.count,
                'tier_routed', CASE tier_rows.tier_routed
                    WHEN 1 THEN json('true') ELSE json('false') END,
                'pct_of_tier_routed', CASE
                    WHEN tier_rows.tier_routed = 1 AND tier_totals.tier_routed_total > 0
                        THEN CAST(tier_rows.count AS REAL) / tier_totals.tier_routed_total
                    ELSE 0.0
                END
            ) AS entry
        FROM tier_rows
        JOIN tier_totals ON tier_totals.tenant_id IS tier_rows.tenant_id
        ORDER BY tier_rows.tenant_id, tier_rows.tier_routed DESC, tier_rows.count DESC,
                 tier_rows.cost_tier_name
    )
    GROUP BY tenant_id
),
by_tier AS (
    SELECT
        tier_totals.tenant_id,
        json_object(
            'total_tasks', tier_totals.total_tasks,
            'tier_routed_total', tier_totals.tier_routed_total,
            'not_tier_routed_count', tier_totals.not_tier_routed_count,
            'local_call_count', tier_totals.local_call_count,
            'total_call_count', tier_totals.total_tasks,
            'local_call_share', CAST(tier_totals.local_call_count AS REAL)
                / NULLIF(tier_totals.total_tasks, 0),
            'tiers', json(COALESCE(tier_list.tiers, '[]'))
        ) AS summary
    FROM tier_totals
    LEFT JOIN tier_list ON tier_list.tenant_id IS tier_totals.tenant_id
)
SELECT
    totals.tenant_id AS tenant_id,
    totals.total_delegations AS total_delegations,
    COALESCE(routing_rows.rows, '[]') AS rows,
    COALESCE(by_model.rows, '[]') AS by_model,
    COALESCE(decision_traces.rows, '[]') AS decision_traces,
    COALESCE(totals.latest_projection_updated_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        AS captured_at,
    1 AS provisioned,
    totals.latest_projection_updated_at AS latest_projection_updated_at,
    by_tier.summary AS by_tier
FROM totals
LEFT JOIN routing_rows ON routing_rows.tenant_id IS totals.tenant_id
LEFT JOIN by_model ON by_model.tenant_id IS totals.tenant_id
LEFT JOIN decision_traces ON decision_traces.tenant_id IS totals.tenant_id
LEFT JOIN by_tier ON by_tier.tenant_id IS totals.tenant_id;
