-- OMN-12839: dashboard-ready routing snapshot over llm_routing_decisions.
--
-- The raw projection table stores one row per routing decision. The dashboard
-- routing-decision widget consumes a single composed snapshot row with model,
-- intent, task preset, and rule arrays. Keep that composition in Postgres so
-- the projection API remains a typed read surface instead of a client-side
-- state owner.

CREATE OR REPLACE VIEW projection_routing_decision AS
WITH totals AS (
    SELECT
        COUNT(*)::int AS total_count,
        MAX(projected_at) AS latest_projection_updated_at
    FROM llm_routing_decisions
),
model_candidates AS (
    SELECT
        llm_agent AS model_id,
        llm_agent AS model_name,
        COALESCE(cost_usd, 0)::float AS cost_usd,
        1 AS sort_order
    FROM llm_routing_decisions
    WHERE llm_agent IS NOT NULL AND llm_agent <> ''
    UNION ALL
    SELECT
        fuzzy_agent AS model_id,
        fuzzy_agent AS model_name,
        0::float AS cost_usd,
        2 AS sort_order
    FROM llm_routing_decisions
    WHERE fuzzy_agent IS NOT NULL AND fuzzy_agent <> ''
),
model_summaries AS (
    SELECT
        model_id,
        model_name,
        MAX(cost_usd)::float AS cost_usd,
        MIN(sort_order) AS sort_order
    FROM model_candidates
    GROUP BY model_id, model_name
),
models AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'id', model_id,
                'name', model_name,
                'tier', CASE WHEN cost_usd = 0 THEN 'local' ELSE 'cloud' END,
                'cost', cost_usd,
                'host', 'unknown'
            )
            ORDER BY sort_order, model_name
        ),
        '[]'::jsonb
    ) AS rows
    FROM model_summaries
),
intent_rows AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'id', intent_id,
                'label', intent_label,
                'color', 'var(--accent)'
            )
            ORDER BY intent_label
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT DISTINCT
            COALESCE(NULLIF(intent, ''), 'unknown') AS intent_id,
            COALESCE(NULLIF(intent, ''), 'Unknown') AS intent_label
        FROM llm_routing_decisions
    ) intents
),
task_presets AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'id', correlation_id::text,
                'label', COALESCE(NULLIF(intent, ''), correlation_id::text),
                'intent', COALESCE(NULLIF(intent, ''), 'unknown'),
                'chosen', llm_agent
            )
            ORDER BY created_at DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT correlation_id, intent, llm_agent, created_at
        FROM llm_routing_decisions
        ORDER BY created_at DESC
        LIMIT 20
    ) recent
),
routing_rules AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'type', intent_label,
                'model', llm_agent,
                'cost', cost_usd,
                'intentId', intent_id
            )
            ORDER BY intent_label, llm_agent
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT
            COALESCE(NULLIF(intent, ''), 'Unknown') AS intent_label,
            COALESCE(NULLIF(intent, ''), 'unknown') AS intent_id,
            llm_agent,
            MIN(COALESCE(cost_usd, 0))::float AS cost_usd
        FROM llm_routing_decisions
        WHERE llm_agent IS NOT NULL AND llm_agent <> ''
        GROUP BY COALESCE(NULLIF(intent, ''), 'Unknown'),
                 COALESCE(NULLIF(intent, ''), 'unknown'),
                 llm_agent
    ) rules
)
SELECT
    COALESCE(models.rows, '[]'::jsonb) AS models,
    COALESCE(intent_rows.rows, '[]'::jsonb) AS intents,
    COALESCE(task_presets.rows, '[]'::jsonb) AS task_presets,
    COALESCE(routing_rules.rows, '[]'::jsonb) AS routing_rules,
    NOW() AS captured_at,
    (totals.total_count > 0) AS provisioned,
    totals.latest_projection_updated_at
FROM totals
CROSS JOIN models
CROSS JOIN intent_rows
CROSS JOIN task_presets
CROSS JOIN routing_rules;
