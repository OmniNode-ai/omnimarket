-- OMN-18159 Phase 1b(ii): the four delegation aggregate views carry their tenant.
--
-- WHAT THIS CLOSES
-- Until now none of these four views had a `tenant_id` column. Each aggregated
-- the whole of `delegation_events` and the WRITER supplied the tenant out of
-- band: it bound the tenant as the session scope, so row-level security
-- filtered the inputs, and separately selected it as a literal column so it
-- could reach the exposure's compaction key and the message header.
--
-- That is correct for exactly one writer shape. The runtime kernel's read seam
-- derives its statement scope from a `tenant_id` FILTER, which cannot be
-- applied to a column the view does not have. With no filter the tenant GUC is
-- left unset, FORCE ROW LEVEL SECURITY hides every underlying row, and the
-- aggregate reports ZEROS -- which on a dashboard reads as a quiet period
-- rather than as a fault. Operator ruling 2026-09-11: re-group on `tenant_id`
-- so the numbers are per-tenant for ANY reader, rather than correct only for a
-- reader who happened to arrive carrying the right session scope.
--
-- The compaction key already said so. OMN-18139 added `tenant_id` to these
-- exposures' `key_columns` to stop a cross-tenant cache collision; the views
-- are the half that never caught up.
--
-- THE TRANSFORMATION, stated so a reviewer can check it rather than trust it:
--   * every CTE that aggregates `delegation_events` gains `tenant_id` in its
--     SELECT and its GROUP BY;
--   * every nested aggregate gains it too, and the enclosing `jsonb_agg`
--     groups by it, so a JSON payload holds one tenant's rows and not a blend;
--   * every `CROSS JOIN` between those CTEs becomes a join ON the tenant --
--     a cross join would now multiply tenants together, which is the one
--     transformation error that would silently produce plausible numbers;
--   * the joins are LEFT JOINs anchored on the `totals`-shaped CTE, because a
--     branch with a WHERE filter (failure categories, for instance) has NO row
--     for a tenant with no failures, and an inner join would drop that tenant
--     from its own aggregate entirely. `COALESCE` restores the empty JSON the
--     ungrouped view produced for the same case.
--
-- `decision_traces` is the one branch that changes meaning rather than shape.
-- It was "the latest 20 delegations in the table"; it is now "the latest 20 of
-- THIS TENANT's delegations", expressed as a window partitioned by tenant
-- rather than a bare LIMIT. A bare LIMIT after grouping would have returned
-- twenty rows shared across all tenants and handed most tenants an empty or
-- truncated trace list, which is a worse answer than the one it replaces.
--
-- FORWARD-ONLY AND IDEMPOTENT, matching 0028's own posture: `CREATE OR REPLACE
-- VIEW` re-points the name. Postgres refuses `CREATE OR REPLACE VIEW` when the
-- output column list changes, and adding `tenant_id` does exactly that, so each
-- view is DROPped first. That is safe here and nowhere near as alarming as it
-- looks: these are views over a table, they hold no data of their own, and
-- every reader of them is re-pointed in the same change.
--
-- GRANTS: re-issued after the drop, for BOTH readers -- see the block at the
-- foot of this file. `DROP VIEW` discards the view's privileges along with the
-- view, so a re-create without them leaves a reader seeing nothing, silently,
-- since a missing privilege reads as an empty page to a reader that cannot
-- distinguish the two. Same re-issue-after-recreate ratchet OMN-14894 records
-- for the RLS policies.

DROP VIEW IF EXISTS projection_delegation_summary;
CREATE VIEW projection_delegation_summary AS
WITH summary AS (
    SELECT
        tenant_id,
        COUNT(*)::int AS total_events,
        COALESCE(SUM(CASE WHEN quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS quality_passed_count,
        COALESCE(SUM(CASE WHEN NOT quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS quality_failed_count,
        COALESCE(AVG(COALESCE(latency_ms, delegation_latency_ms)), 0)::float
            AS avg_latency_ms,
        COALESCE(MAX(EXTRACT(EPOCH FROM (created_at))), 0)::float AS latest_event_at,
        COALESCE(SUM(cost_savings_usd), 0)::float AS total_savings_usd,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
    GROUP BY tenant_id
),
by_task_type AS (
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(jsonb_build_object('taskType', task_type, 'count', count)
                ORDER BY count DESC),
            '[]'::jsonb
        ) AS rows
    FROM (
        SELECT tenant_id, task_type, COUNT(*)::int AS count
        FROM delegation_events
        GROUP BY tenant_id, task_type
    ) grouped
    GROUP BY tenant_id
),
by_model AS (
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(jsonb_build_object('model', delegated_to, 'count', count)
                ORDER BY count DESC),
            '[]'::jsonb
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
    CASE WHEN summary.total_events > 0
        THEN summary.quality_passed_count::float / summary.total_events
        ELSE 0
    END AS "qualityGatePassRate",
    summary.quality_passed_count AS "qualityGatePassed",
    summary.total_events AS "qualityGateTotal",
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
    summary.latest_projection_updated_at
FROM summary
LEFT JOIN by_task_type USING (tenant_id)
LEFT JOIN by_model USING (tenant_id);

DROP VIEW IF EXISTS projection_delegation_model_routing;
CREATE VIEW projection_delegation_model_routing AS
WITH grouped AS (
    SELECT
        tenant_id,
        delegated_to AS model_alias,
        COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name,
        task_type,
        COUNT(*)::int AS event_count,
        COALESCE(SUM(CASE WHEN quality_gate_passed THEN 1 ELSE 0 END), 0)::int
            AS quality_passed,
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
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'model_name', model_totals.model_alias,
                    'total_count', model_totals.total_count,
                    'pct_of_total', CASE WHEN totals.total_delegations > 0
                        THEN model_totals.total_count::float / totals.total_delegations
                        ELSE 0 END,
                    'top_task_type', model_totals.top_task_type,
                    'avg_latency_ms', model_totals.avg_latency_ms,
                    'qg_pass_rate', CASE WHEN model_totals.total_count > 0
                        THEN model_totals.quality_passed::float / model_totals.total_count
                        ELSE 0 END,
                    'task_types', model_totals.task_types
                ) ORDER BY model_totals.total_count DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM model_totals
    JOIN totals USING (tenant_id)
    GROUP BY model_totals.tenant_id
),
routing_rows AS (
    SELECT
        grouped.tenant_id,
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'model_name', grouped.model_alias,
                    'task_type', grouped.task_type,
                    'count', grouped.event_count,
                    'pct_of_model', CASE WHEN model_totals.total_count > 0
                        THEN grouped.event_count::float / model_totals.total_count
                        ELSE 0 END,
                    'pct_of_total', CASE WHEN totals.total_delegations > 0
                        THEN grouped.event_count::float / totals.total_delegations
                        ELSE 0 END
                ) ORDER BY grouped.event_count DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM grouped
    JOIN model_totals USING (tenant_id, model_alias)
    JOIN totals USING (tenant_id)
    GROUP BY grouped.tenant_id
),
decision_traces AS (
    SELECT
        tenant_id,
        COALESCE(
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
                    'created_at', EXTRACT(EPOCH FROM (created_at))
                ) ORDER BY created_at DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM (
        SELECT
            tenant_id,
            row_number() OVER (
                PARTITION BY tenant_id ORDER BY created_at DESC
            )::int AS id,
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
            AS not_tier_routed_count
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
by_tier AS (
    SELECT
        tier_totals.tenant_id,
        jsonb_build_object(
            'total_tasks', tier_totals.total_tasks,
            'tier_routed_total', tier_totals.tier_routed_total,
            'not_tier_routed_count', tier_totals.not_tier_routed_count,
            'tiers', COALESCE(
                (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'cost_tier_name', tier_rows.cost_tier_name,
                            'count', tier_rows.count,
                            'tier_routed', tier_rows.tier_routed,
                            'pct_of_tier_routed', CASE
                                WHEN tier_rows.tier_routed
                                     AND tier_totals.tier_routed_total > 0
                                    THEN tier_rows.count::float
                                         / tier_totals.tier_routed_total
                                ELSE 0
                            END
                        ) ORDER BY tier_rows.tier_routed DESC,
                                   tier_rows.count DESC,
                                   tier_rows.cost_tier_name
                    )
                    FROM tier_rows
                    -- Correlated on the tenant: without this the subquery
                    -- would fold every tenant's tiers into every tenant's row.
                    WHERE tier_rows.tenant_id = tier_totals.tenant_id
                ),
                '[]'::jsonb
            )
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

DROP VIEW IF EXISTS projection_delegation_quality_gate;
CREATE VIEW projection_delegation_quality_gate AS
WITH totals AS (
    SELECT
        tenant_id,
        COUNT(*) FILTER (WHERE quality_gate_passed IS NOT NULL)::int AS total_checks,
        COUNT(*) FILTER (WHERE quality_gate_passed IS TRUE)::int AS total_passed,
        COUNT(*) FILTER (WHERE quality_gate_passed IS FALSE)::int AS total_failed,
        COALESCE(SUM(escalation_count), 0)::int AS total_escalations,
        COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0)::float
            AS avg_tokens_to_compliance,
        percentile_cont(0.5) WITHIN GROUP (
            ORDER BY NULLIF(tokens_to_compliance, 0)
        ) AS median_tokens_to_compliance,
        COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1)::float
            AS avg_compliance_attempts,
        COALESCE(AVG(actual_score), 0)::float AS avg_actual_score,
        COALESCE(AVG(required_bar), 0)::float AS avg_required_bar,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
    GROUP BY tenant_id
),
failure_categories AS (
    SELECT
        failures.tenant_id,
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'category', failures.quality_gate_detail,
                    'count', failures.failed_count,
                    'pct_of_failures', CASE WHEN totals.total_failed > 0
                        THEN failures.failed_count::float / totals.total_failed
                        ELSE 0 END
                ) ORDER BY failures.failed_count DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM (
        SELECT tenant_id, quality_gate_detail, COUNT(*)::int AS failed_count
        FROM delegation_events
        WHERE quality_gate_detail IS NOT NULL
          AND NOT quality_gate_passed
        GROUP BY tenant_id, quality_gate_detail
    ) failures
    JOIN totals USING (tenant_id)
    GROUP BY failures.tenant_id
),
tokens_by_model AS (
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'model_name', model_name,
                    'avg_tokens', avg_tokens,
                    'avg_attempts', avg_attempts,
                    'sample_count', sample_count
                ) ORDER BY avg_tokens DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM (
        SELECT
            tenant_id,
            COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name,
            COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0)::float AS avg_tokens,
            COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1)::float AS avg_attempts,
            COUNT(*)::int AS sample_count
        FROM delegation_events
        GROUP BY tenant_id, COALESCE(NULLIF(model_name, ''), delegated_to)
    ) by_model
    GROUP BY tenant_id
)
SELECT
    totals.tenant_id,
    CASE WHEN totals.total_checks > 0
        THEN totals.total_passed::float / totals.total_checks ELSE 0 END
        AS overall_pass_rate,
    totals.total_passed,
    totals.total_failed,
    totals.total_checks,
    totals.total_escalations AS escalation_count,
    CASE WHEN totals.total_checks > 0
        THEN totals.total_escalations::float / totals.total_checks ELSE 0 END
        AS escalation_rate,
    jsonb_build_array(jsonb_build_object(
        'check_type', 'score_vs_required_bar',
        'passed', totals.total_passed,
        'failed', totals.total_failed,
        'total', totals.total_checks,
        'pass_rate', CASE WHEN totals.total_checks > 0
            THEN totals.total_passed::float / totals.total_checks ELSE 0 END
    )) AS by_check_type,
    COALESCE(failure_categories.rows, '[]'::jsonb) AS failure_categories,
    totals.avg_tokens_to_compliance,
    totals.median_tokens_to_compliance,
    totals.avg_compliance_attempts,
    COALESCE(tokens_by_model.rows, '[]'::jsonb) AS tokens_to_compliance_by_model,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at,
    totals.avg_actual_score,
    totals.avg_required_bar
FROM totals
LEFT JOIN failure_categories USING (tenant_id)
LEFT JOIN tokens_by_model USING (tenant_id);

DROP VIEW IF EXISTS projection_delegation_token_usage;
CREATE VIEW projection_delegation_token_usage AS
WITH model_usage AS (
    SELECT
        tenant_id,
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
    GROUP BY tenant_id,
             COALESCE(NULLIF(delegated_to, ''), NULLIF(model_name, ''), 'delegated-runtime'),
             COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'delegated-runtime')
),
totals AS (
    SELECT
        tenant_id,
        COALESCE(SUM(tokens_input), 0)::int AS total_prompt_tokens,
        COALESCE(SUM(tokens_output), 0)::int AS total_completion_tokens,
        COALESCE(SUM(tokens_input + tokens_output), 0)::int AS total_tokens,
        COALESCE(SUM(cost_usd), 0)::float AS total_estimated_cost_usd,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
    GROUP BY tenant_id
),
by_model AS (
    SELECT
        tenant_id,
        COALESCE(
            -- `- 'tenant_id'` keeps the per-model JSON payload byte-identical
            -- to what the ungrouped view produced. The grouping column is the
            -- ROW's identity, not a property of the model, and leaking it into
            -- every element would change a shape the dashboard already parses.
            jsonb_agg((to_jsonb(model_usage) - 'tenant_id') ORDER BY total_tokens DESC),
            '[]'::jsonb
        ) AS rows
    FROM model_usage
    GROUP BY tenant_id
),
provenance AS (
    SELECT
        tenant_id,
        jsonb_build_object(
            'measured', COALESCE(SUM(CASE WHEN token_provenance = 'measured' THEN 1 ELSE 0 END), 0),
            'estimated', COALESCE(SUM(CASE WHEN token_provenance = 'estimated' THEN 1 ELSE 0 END), 0),
            'unknown', COALESCE(SUM(CASE WHEN token_provenance = 'unknown' THEN 1 ELSE 0 END), 0)
        ) AS summary
    FROM model_usage
    GROUP BY tenant_id
)
SELECT
    totals.tenant_id,
    totals.total_prompt_tokens,
    totals.total_completion_tokens,
    totals.total_tokens,
    totals.total_estimated_cost_usd,
    provenance.summary AS provenance_summary,
    COALESCE(by_model.rows, '[]'::jsonb) AS by_model,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at
FROM totals
LEFT JOIN provenance USING (tenant_id)
LEFT JOIN by_model USING (tenant_id);

-- Re-issue every reader grant on all four. DROP VIEW discarded them along with
-- the views, and a missing privilege reads as an empty page to a reader that
-- cannot tell the two apart -- the same confident-empty failure this whole
-- phase exists to remove.
--
-- BOTH readers are named here, and the second is the one this migration adds.
-- `app_dashboard` read these views already. `tenant_projection_writer` is the
-- principal the runtime kernel resolves for the tenant-domain binding, and it
-- now READS them too, because the in-process publisher re-reads each aggregate
-- after a write in order to republish it. Declaring that grant in the topology
-- without delivering it here would be a capability the runtime advertises and
-- the database refuses -- an outage waiting for the relation to take traffic,
-- which is exactly what the grant-delivery gate exists to stop.
--
-- LITERAL grants, not a dynamic DO block, and the reason is mechanical rather
-- than stylistic. The topology grant-delivery gate reads these files looking
-- for the GRANT that delivers each declared privilege; a grant hidden inside
-- `EXECUTE format(...)` is invisible to it, so the declaration would read as
-- undelivered -- which the gate calls an outage waiting for the relation to
-- take traffic. Both roles exist wherever this chain applies: app_dashboard is
-- provisioned by the RLS migrations earlier in this lineage, and
-- tenant_projection_writer by node_projection_delegation_inference_response/
-- 0004.
GRANT SELECT ON projection_delegation_summary TO app_dashboard;
GRANT SELECT ON projection_delegation_model_routing TO app_dashboard;
GRANT SELECT ON projection_delegation_quality_gate TO app_dashboard;
GRANT SELECT ON projection_delegation_token_usage TO app_dashboard;
GRANT SELECT ON projection_delegation_summary TO tenant_projection_writer;
GRANT SELECT ON projection_delegation_model_routing TO tenant_projection_writer;
GRANT SELECT ON projection_delegation_quality_gate TO tenant_projection_writer;
GRANT SELECT ON projection_delegation_token_usage TO tenant_projection_writer;
