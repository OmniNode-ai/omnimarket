-- OMN-12489: authoritative dashboard read views for bus-fed delegation projections.

CREATE OR REPLACE VIEW projection_delegation_summary AS
WITH summary AS (
    SELECT
        COUNT(*)::int AS total_events,
        COALESCE(SUM(CASE WHEN quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS quality_passed_count,
        COALESCE(SUM(CASE WHEN NOT quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS quality_failed_count,
        COALESCE(AVG(COALESCE(latency_ms, delegation_latency_ms)), 0)::float
            AS avg_latency_ms,
        COALESCE(MAX(EXTRACT(EPOCH FROM created_at)), 0)::float AS latest_event_at,
        COALESCE(SUM(cost_savings_usd), 0)::float AS total_savings_usd,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
),
by_task_type AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object('taskType', task_type, 'count', count)
            ORDER BY count DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT task_type, COUNT(*)::int AS count
        FROM delegation_events
        GROUP BY task_type
    ) t
),
by_model AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object('model', delegated_to, 'count', count)
            ORDER BY count DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT delegated_to, COUNT(*)::int AS count
        FROM delegation_events
        GROUP BY delegated_to
    ) t
)
SELECT
    s.total_events AS "totalDelegations",
    CASE WHEN s.total_events > 0
        THEN s.quality_passed_count::float / s.total_events
        ELSE 0
    END AS "qualityGatePassRate",
    s.quality_passed_count AS "qualityGatePassed",
    s.total_events AS "qualityGateTotal",
    s.total_savings_usd AS "totalSavingsUsd",
    s.avg_latency_ms AS "avgLatencyMs",
    s.latest_event_at AS "latestEventAt",
    s.total_events,
    s.quality_passed_count,
    s.quality_failed_count,
    s.avg_latency_ms,
    s.latest_event_at,
    by_task_type.rows AS "byTaskType",
    by_model.rows AS "byModel",
    s.latest_projection_updated_at
FROM summary s
CROSS JOIN by_task_type
CROSS JOIN by_model;

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
)
SELECT
    totals.total_delegations,
    routing_rows.rows,
    by_model.rows AS by_model,
    decision_traces.rows AS decision_traces,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at
FROM totals
CROSS JOIN routing_rows
CROSS JOIN by_model
CROSS JOIN decision_traces;

CREATE OR REPLACE VIEW projection_delegation_quality_gate AS
WITH totals AS (
    SELECT
        COUNT(*)::int AS total_checks,
        COALESCE(SUM(CASE WHEN quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS total_passed,
        COALESCE(SUM(CASE WHEN NOT quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS total_failed,
        COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0)::float
            AS avg_tokens_to_compliance,
        percentile_cont(0.5) WITHIN GROUP (
            ORDER BY NULLIF(tokens_to_compliance, 0)
        ) AS median_tokens_to_compliance,
        COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1)::float
            AS avg_compliance_attempts,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
),
failure_categories AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'category', quality_gate_detail,
                'count', failed_count,
                'pct_of_failures', CASE WHEN totals.total_failed > 0
                    THEN failed_count::float / totals.total_failed ELSE 0 END
            )
            ORDER BY failed_count DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT quality_gate_detail, COUNT(*)::int AS failed_count
        FROM delegation_events
        WHERE quality_gate_detail IS NOT NULL
          AND NOT quality_gate_passed
        GROUP BY quality_gate_detail
    ) failures
    CROSS JOIN totals
),
tokens_by_model AS (
    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'model_name', model_name,
                'avg_tokens', avg_tokens,
                'avg_attempts', avg_attempts,
                'sample_count', sample_count
            )
            ORDER BY avg_tokens DESC
        ),
        '[]'::jsonb
    ) AS rows
    FROM (
        SELECT
            COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name,
            COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0)::float AS avg_tokens,
            COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1)::float AS avg_attempts,
            COUNT(*)::int AS sample_count
        FROM delegation_events
        GROUP BY COALESCE(NULLIF(model_name, ''), delegated_to)
    ) by_model
)
SELECT
    CASE WHEN totals.total_checks > 0
        THEN totals.total_passed::float / totals.total_checks ELSE 0 END
        AS overall_pass_rate,
    totals.total_passed,
    totals.total_failed,
    totals.total_checks,
    totals.total_failed AS escalation_count,
    CASE WHEN totals.total_checks > 0
        THEN totals.total_failed::float / totals.total_checks ELSE 0 END
        AS escalation_rate,
    jsonb_build_array(jsonb_build_object(
        'check_type', 'deterministic',
        'passed', totals.total_passed,
        'failed', totals.total_failed,
        'total', totals.total_checks,
        'pass_rate', CASE WHEN totals.total_checks > 0
            THEN totals.total_passed::float / totals.total_checks ELSE 0 END
    )) AS by_check_type,
    failure_categories.rows AS failure_categories,
    totals.avg_tokens_to_compliance,
    totals.median_tokens_to_compliance,
    totals.avg_compliance_attempts,
    tokens_by_model.rows AS tokens_to_compliance_by_model,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at
FROM totals
CROSS JOIN failure_categories
CROSS JOIN tokens_by_model;

CREATE OR REPLACE VIEW projection_delegation_token_usage AS
WITH model_usage AS (
    SELECT
        COALESCE(NULLIF(delegated_to, ''), NULLIF(model_name, ''), 'delegated-runtime')
            AS model_id,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'delegated-runtime')
            AS model_name,
        COALESCE(SUM(tokens_input), 0)::int AS prompt_tokens,
        COALESCE(SUM(tokens_output), 0)::int AS completion_tokens,
        COALESCE(SUM(tokens_input + tokens_output), 0)::int AS total_tokens,
        COALESCE(SUM(cost_usd), 0)::float AS estimated_cost_usd,
        CASE
            WHEN COALESCE(SUM(tokens_input + tokens_output), 0) > 0 THEN 'measured'
            WHEN COALESCE(SUM(cost_usd), 0) > 0 THEN 'estimated'
            ELSE 'unknown'
        END AS usage_source,
        CASE
            WHEN COALESCE(SUM(tokens_input + tokens_output), 0) > 0 THEN 'measured'
            WHEN COALESCE(SUM(cost_usd), 0) > 0 THEN 'estimated'
            ELSE 'unknown'
        END AS token_provenance
    FROM delegation_events
    GROUP BY COALESCE(NULLIF(delegated_to, ''), NULLIF(model_name, ''), 'delegated-runtime'),
             COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'delegated-runtime')
),
totals AS (
    SELECT
        COALESCE(SUM(tokens_input), 0)::int AS total_prompt_tokens,
        COALESCE(SUM(tokens_output), 0)::int AS total_completion_tokens,
        COALESCE(SUM(tokens_input + tokens_output), 0)::int AS total_tokens,
        COALESCE(SUM(cost_usd), 0)::float AS total_estimated_cost_usd,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
),
by_model AS (
    SELECT COALESCE(
        jsonb_agg(to_jsonb(model_usage) ORDER BY total_tokens DESC),
        '[]'::jsonb
    ) AS rows
    FROM model_usage
),
provenance AS (
    SELECT jsonb_build_object(
        'measured', COALESCE(SUM(CASE WHEN token_provenance = 'measured' THEN 1 ELSE 0 END), 0),
        'estimated', COALESCE(SUM(CASE WHEN token_provenance = 'estimated' THEN 1 ELSE 0 END), 0),
        'unknown', COALESCE(SUM(CASE WHEN token_provenance = 'unknown' THEN 1 ELSE 0 END), 0)
    ) AS summary
    FROM model_usage
)
SELECT
    totals.total_prompt_tokens,
    totals.total_completion_tokens,
    totals.total_tokens,
    totals.total_estimated_cost_usd,
    provenance.summary AS provenance_summary,
    by_model.rows AS by_model,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at
FROM totals
CROSS JOIN provenance
CROSS JOIN by_model;
