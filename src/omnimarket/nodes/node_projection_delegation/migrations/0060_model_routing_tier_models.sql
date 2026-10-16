-- OMN-21015 AC2: the model-routing view lists, under each tier in by_tier, the
-- models routed under it (Jonah's ruling on OMN-21015, 2026-10-14T04:50Z: "taking
-- the cost from the view, summed per tier and model with an unmeasured count beside
-- it"). Each tier entry gains one key, models, an array of
--   model_name             delegated_to, the same name by_model serves
--   count                  runs of that model under that tier
--   pct_of_tier_routed     count / tier_routed_total, the tier's own denominator,
--                          so a tier's model shares sum to the tier's share; 0 for
--                          the not_tier_routed bucket, as the tier itself
--   cost_usd               SUM(cost_usd) over the group's runs; NULL, never 0, when
--                          no run of the group carries a cost
--   unmeasured_cost_count  runs of the group with no cost
-- ordered by count, then name. Every existing key and column is unchanged.
--
-- The body is 0055's projection_delegation_model_routing with that key added.
-- security_invoker: CREATE OR REPLACE VIEW drops it, so it is set again below,
-- or the view would stop inheriting delegation_events' row-level security.
-- The local store's counterpart is sqlite/delegation_model_routing_view_v2.sql.

BEGIN;

CREATE OR REPLACE VIEW projection_delegation_model_routing AS
WITH grouped AS (
    SELECT
        tenant_id,
        delegated_to AS model_alias,
        COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name,
        task_type,
        COUNT(*)::int AS event_count,
        COALESCE(SUM(CASE WHEN quality_gate_passed
            AND (operational_outcome, content_verdict) IS DISTINCT FROM
                ('terminal_construction_failed', 'undetermined')
            THEN 1 ELSE 0 END), 0)::int AS quality_passed,
        COUNT(*) FILTER (WHERE quality_gate_passed IS NOT NULL
            AND (operational_outcome, content_verdict) IS DISTINCT FROM
                ('terminal_construction_failed', 'undetermined'))::int AS quality_checks,
        COALESCE(AVG(COALESCE(latency_ms, delegation_latency_ms)), 0)::float
            AS avg_latency_ms
    FROM delegation_events
    GROUP BY tenant_id, delegated_to,
             COALESCE(NULLIF(model_name, ''), delegated_to), task_type
),
totals AS (
    SELECT tenant_id,
           COUNT(*)::int AS total_delegations,
           MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
    GROUP BY tenant_id
),
model_totals AS (
    SELECT
        tenant_id,
        model_alias,
        SUM(event_count)::int AS total_count,
        SUM(quality_passed)::int AS quality_passed,
        SUM(quality_checks)::int AS quality_checks,
        CASE WHEN SUM(event_count) > 0
            THEN SUM(avg_latency_ms * event_count)::float / SUM(event_count)
            ELSE 0
        END AS avg_latency_ms,
        (array_agg(task_type ORDER BY event_count DESC))[1] AS top_task_type,
        to_jsonb(array_agg(task_type ORDER BY task_type)) AS task_types
    FROM grouped
    GROUP BY tenant_id, model_alias
),
by_model AS (
    SELECT
        model_totals.tenant_id,
        COALESCE(jsonb_agg(jsonb_build_object(
            'model_name', model_totals.model_alias,
            'total_count', model_totals.total_count,
            'pct_of_total', CASE WHEN totals.total_delegations > 0
                THEN model_totals.total_count::float / totals.total_delegations
                ELSE 0 END,
            'top_task_type', model_totals.top_task_type,
            'avg_latency_ms', model_totals.avg_latency_ms,
            'qg_pass_rate', CASE WHEN model_totals.quality_checks > 0
                THEN model_totals.quality_passed::float / model_totals.quality_checks
                ELSE 0 END,
            'task_types', model_totals.task_types
        ) ORDER BY model_totals.total_count DESC), '[]'::jsonb) AS rows
    FROM model_totals
    JOIN totals USING (tenant_id)
    GROUP BY model_totals.tenant_id
),
routing_rows AS (
    SELECT
        grouped.tenant_id,
        COALESCE(jsonb_agg(jsonb_build_object(
            'model_name', grouped.model_alias,
            'task_type', grouped.task_type,
            'count', grouped.event_count,
            'pct_of_model', CASE WHEN model_totals.total_count > 0
                THEN grouped.event_count::float / model_totals.total_count
                ELSE 0 END,
            'pct_of_total', CASE WHEN totals.total_delegations > 0
                THEN grouped.event_count::float / totals.total_delegations
                ELSE 0 END
        ) ORDER BY grouped.event_count DESC), '[]'::jsonb) AS rows
    FROM grouped
    JOIN model_totals USING (tenant_id, model_alias)
    JOIN totals USING (tenant_id)
    GROUP BY grouped.tenant_id
),
decision_traces AS (
    SELECT
        tenant_id,
        COALESCE(jsonb_agg(jsonb_build_object(
            'id', id,
            'correlation_id', correlation_id,
            'task_type', task_type,
            'model_name', COALESCE(NULLIF(model_name, ''), delegated_to),
            'delegated_to', delegated_to,
            'routing_rule', NULL,
            'routing_confidence', NULL,
            'routing_candidates', NULL,
            'latency_ms', COALESCE(latency_ms, delegation_latency_ms),
            'quality_gate_passed', quality_gate_passed,
            'created_at', EXTRACT(EPOCH FROM (created_at))
        ) ORDER BY created_at DESC), '[]'::jsonb) AS rows
    FROM (
        SELECT
            tenant_id,
            row_number() OVER (PARTITION BY tenant_id ORDER BY created_at DESC)::int AS id,
            correlation_id,
            task_type,
            model_name,
            delegated_to,
            latency_ms,
            delegation_latency_ms,
            quality_gate_passed,
            created_at
        FROM delegation_events
    ) ranked
    WHERE id <= 20
    GROUP BY tenant_id
),
tier_totals AS (
    SELECT
        tenant_id,
        COUNT(*)::int AS total_tasks,
        COALESCE(SUM(CASE WHEN cost_tier_name <> '' THEN 1 ELSE 0 END), 0)::int
            AS tier_routed_total,
        COALESCE(SUM(CASE WHEN cost_tier_name = '' THEN 1 ELSE 0 END), 0)::int
            AS not_tier_routed_count,
        COUNT(*) FILTER (WHERE cost_tier_name = 'local')::int AS local_call_count
    FROM delegation_events
    GROUP BY tenant_id
),
tier_rows AS (
    SELECT
        tenant_id,
        CASE WHEN cost_tier_name = '' THEN 'not_tier_routed' ELSE cost_tier_name END
            AS cost_tier_name,
        (cost_tier_name <> '') AS tier_routed,
        COUNT(*)::int AS count
    FROM delegation_events
    GROUP BY tenant_id, 2, 3
),
tier_model_rows AS (
    SELECT
        tenant_id,
        CASE WHEN cost_tier_name = '' THEN 'not_tier_routed' ELSE cost_tier_name END
            AS cost_tier_name,
        (cost_tier_name <> '') AS tier_routed,
        delegated_to AS model_alias,
        COUNT(*)::int AS count,
        SUM(cost_usd) AS cost_usd,
        COUNT(*) FILTER (WHERE cost_usd IS NULL)::int AS unmeasured_cost_count
    FROM delegation_events
    GROUP BY tenant_id, 2, 3, 4
),
by_tier AS (
    SELECT
        tier_totals.tenant_id,
        jsonb_build_object(
            'total_tasks', tier_totals.total_tasks,
            'tier_routed_total', tier_totals.tier_routed_total,
            'not_tier_routed_count', tier_totals.not_tier_routed_count,
            'local_call_count', tier_totals.local_call_count,
            'total_call_count', tier_totals.total_tasks,
            'local_call_share', tier_totals.local_call_count::float
                / NULLIF(tier_totals.total_tasks, 0),
            'tiers', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'cost_tier_name', tier_rows.cost_tier_name,
                    'count', tier_rows.count,
                    'tier_routed', tier_rows.tier_routed,
                    'pct_of_tier_routed', CASE
                        WHEN tier_rows.tier_routed
                            AND tier_totals.tier_routed_total > 0
                            THEN tier_rows.count::float / tier_totals.tier_routed_total
                        ELSE 0
                    END,
                    'models', COALESCE((
                        SELECT jsonb_agg(jsonb_build_object(
                            'model_name', tier_model_rows.model_alias,
                            'count', tier_model_rows.count,
                            'pct_of_tier_routed', CASE
                                WHEN tier_model_rows.tier_routed
                                    AND tier_totals.tier_routed_total > 0
                                    THEN tier_model_rows.count::float
                                        / tier_totals.tier_routed_total
                                ELSE 0
                            END,
                            'cost_usd', tier_model_rows.cost_usd,
                            'unmeasured_cost_count', tier_model_rows.unmeasured_cost_count
                        ) ORDER BY tier_model_rows.count DESC, tier_model_rows.model_alias)
                        FROM tier_model_rows
                        WHERE tier_model_rows.tenant_id = tier_rows.tenant_id
                            AND tier_model_rows.cost_tier_name = tier_rows.cost_tier_name
                    ), '[]'::jsonb)
                ) ORDER BY tier_rows.tier_routed DESC, tier_rows.count DESC,
                           tier_rows.cost_tier_name)
                FROM tier_rows
                WHERE tier_rows.tenant_id = tier_totals.tenant_id
            ), '[]'::jsonb)
        ) AS summary
    FROM tier_totals
)
SELECT
    totals.tenant_id,
    totals.total_delegations,
    COALESCE(routing_rows.rows, '[]'::jsonb) AS rows,
    COALESCE(by_model.rows, '[]'::jsonb) AS by_model,
    COALESCE(decision_traces.rows, '[]'::jsonb) AS decision_traces,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at,
    by_tier.summary AS by_tier
FROM totals
LEFT JOIN routing_rows USING (tenant_id)
LEFT JOIN by_model USING (tenant_id)
LEFT JOIN decision_traces USING (tenant_id)
LEFT JOIN by_tier USING (tenant_id);

ALTER VIEW projection_delegation_model_routing SET (security_invoker = true);

COMMIT;
