-- OMN-13662 (T3): explicit not_tier_routed classification in the delegation
-- model-routing rollup.
--
-- The delegate-skill terminal path (ModelDelegateSkillTerminalProjection) is
-- skill->node delegation, NOT LLM-tier routing, so those delegation_events rows
-- legitimately carry an empty cost_tier_name (TEXT NOT NULL DEFAULT '' from
-- migration 0018). The tier rollup must define HOW empty-tier rows are treated
-- rather than leaving it implementation-defined (which is what produced the old
-- client-side regex).
--
-- Authoritative projection rule (doctrine: no silent default):
--   * map empty cost_tier_name -> the explicit value 'not_tier_routed';
--   * EXCLUDE not_tier_routed rows from tier-distribution percentage
--     DENOMINATORS (pct is computed over LLM-tier-routed rows only, so the
--     tier-routed percentages sum to 1.0);
--   * INCLUDE not_tier_routed rows in total task counts (total_tasks);
--   * surface the classification + the excluded count on the projection so the
--     dashboard READS it and never re-derives it.
--
-- This is a CREATE OR REPLACE VIEW (psql-applicable, no node redeploy). The new
-- by_tier column is appended at the END of the SELECT list so the replace is
-- compatible with the existing projection_delegation_model_routing view defined
-- in migration 0010.

CREATE OR REPLACE VIEW projection_delegation_model_routing AS
WITH grouped AS (
    SELECT
        delegated_to AS model_alias,
        COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name,
        task_type,
        COUNT(*)::int AS event_count,
        COALESCE(SUM(CASE WHEN quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS quality_passed,
        COALESCE(AVG(COALESCE(latency_ms, delegation_latency_ms)), 0)::float
            AS avg_latency_ms
    FROM delegation_events
    GROUP BY delegated_to, COALESCE(NULLIF(model_name, ''), delegated_to), task_type
),
totals AS (
    SELECT COUNT(*)::int AS total_delegations, MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
),
by_model AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'model_name', model_alias,
                'total_count', total_count,
                'pct_of_total', CASE WHEN totals.total_delegations > 0
                    THEN total_count::float / totals.total_delegations ELSE 0 END,
                'top_task_type', top_task_type,
                'avg_latency_ms', avg_latency_ms,
                'qg_pass_rate', CASE WHEN total_count > 0
                    THEN quality_passed::float / total_count ELSE 0 END,
                'task_types', task_types
            )
            ORDER BY total_count DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT
            model_alias,
            SUM(event_count)::int AS total_count,
            SUM(quality_passed)::int AS quality_passed,
            CASE WHEN SUM(event_count) > 0
                THEN SUM(avg_latency_ms * event_count)::float / SUM(event_count)
                ELSE 0
            END AS avg_latency_ms,
            (array_agg(task_type ORDER BY event_count DESC))[1] AS top_task_type,
            to_jsonb(array_agg(task_type ORDER BY task_type)) AS task_types
        FROM grouped
        GROUP BY model_alias
    ) m
    CROSS JOIN totals
),
routing_rows AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'model_name', g.model_alias,
                'task_type', g.task_type,
                'count', g.event_count,
                'pct_of_model', CASE WHEN model_totals.total_count > 0
                    THEN g.event_count::float / model_totals.total_count ELSE 0 END,
                'pct_of_total', CASE WHEN totals.total_delegations > 0
                    THEN g.event_count::float / totals.total_delegations ELSE 0 END
            )
            ORDER BY g.event_count DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM grouped g
    JOIN (
        SELECT model_alias, SUM(event_count)::int AS total_count
        FROM grouped
        GROUP BY model_alias
    ) model_totals USING (model_alias)
    CROSS JOIN totals
),
decision_traces AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
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
                'created_at', EXTRACT(EPOCH FROM created_at)
            )
            ORDER BY created_at DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT
            row_number() OVER (ORDER BY created_at DESC)::int AS id,
            correlation_id,
            task_type,
            model_name,
            delegated_to,
            latency_ms,
            delegation_latency_ms,
            quality_gate_passed,
            created_at
        FROM delegation_events
        ORDER BY created_at DESC
        LIMIT 20
    ) traces
),
-- OMN-13662: tier distribution with explicit not_tier_routed classification.
-- A row is "tier-routed" iff cost_tier_name is non-empty. Empty-tier rows are
-- the delegate-skill terminals (skill->node, not LLM-tier routing).
tier_totals AS (
    SELECT
        COUNT(*)::int AS total_tasks,
        COALESCE(SUM(CASE WHEN cost_tier_name <> '' THEN 1 ELSE 0 END), 0)::int
            AS tier_routed_total,
        COALESCE(SUM(CASE WHEN cost_tier_name = '' THEN 1 ELSE 0 END), 0)::int
            AS not_tier_routed_count
    FROM delegation_events
),
tier_rows AS (
    SELECT
        CASE WHEN cost_tier_name = '' THEN 'not_tier_routed' ELSE cost_tier_name END
            AS cost_tier_name,
        (cost_tier_name <> '') AS tier_routed,
        COUNT(*)::int AS count
    FROM delegation_events
    GROUP BY 1, 2
),
by_tier AS (
    SELECT jsonb_build_object(
        -- inclusive of not_tier_routed rows
        'total_tasks', tt.total_tasks,
        -- denominator for tier percentages (LLM-tier-routed rows only)
        'tier_routed_total', tt.tier_routed_total,
        -- delegate-skill terminals excluded from the percentage denominator
        'not_tier_routed_count', tt.not_tier_routed_count,
        'tiers', COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'cost_tier_name', tr.cost_tier_name,
                        'count', tr.count,
                        'tier_routed', tr.tier_routed,
                        -- pct over LLM-tier-routed rows only; 0 for the
                        -- excluded not_tier_routed bucket so the tier-routed
                        -- percentages sum to 1.0
                        'pct_of_tier_routed', CASE
                            WHEN tr.tier_routed AND tt.tier_routed_total > 0
                                THEN tr.count::float / tt.tier_routed_total
                            ELSE 0
                        END
                    )
                    ORDER BY tr.tier_routed DESC, tr.count DESC, tr.cost_tier_name
                )
                FROM tier_rows tr
            ),
            '[]'::jsonb
        )
    ) AS summary
    FROM tier_totals tt
)
SELECT
    totals.total_delegations,
    routing_rows.rows,
    by_model.rows AS by_model,
    decision_traces.rows AS decision_traces,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at,
    by_tier.summary AS by_tier
FROM totals
CROSS JOIN routing_rows
CROSS JOIN by_model
CROSS JOIN decision_traces
CROSS JOIN by_tier;
