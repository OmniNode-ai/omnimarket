-- OMN-22573: rollback of 0065. Restores the view of migration 0045, whose
-- failure_categories is unbounded.
--
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0065's ledger row, so the node
-- loop will not re-apply 0065 unless that row is removed too. Run it as the
-- role that ran 0065.

BEGIN;

CREATE OR REPLACE VIEW projection_delegation_quality_gate AS
WITH eligible AS (
    SELECT * FROM delegation_events
    WHERE (operational_outcome, content_verdict) IS DISTINCT FROM
        ('terminal_construction_failed', 'undetermined')
), totals AS (
    SELECT
        tenant_id,
        COUNT(*) FILTER (WHERE quality_gate_passed IS NOT NULL)::int AS total_checks,
        COUNT(*) FILTER (WHERE quality_gate_passed IS TRUE)::int AS total_passed,
        COUNT(*) FILTER (WHERE quality_gate_passed IS FALSE)::int AS total_failed,
        COALESCE(SUM(escalation_count), 0)::int AS total_escalations,
        COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0)::float AS avg_tokens_to_compliance,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY NULLIF(tokens_to_compliance, 0))
            AS median_tokens_to_compliance,
        COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1)::float AS avg_compliance_attempts,
        COALESCE(AVG(actual_score), 0)::float AS avg_actual_score,
        COALESCE(AVG(required_bar), 0)::float AS avg_required_bar,
        MAX(created_at) AS latest_projection_updated_at
    FROM eligible
    GROUP BY tenant_id
),
failure_categories AS (
    SELECT
        failures.tenant_id,
        COALESCE(jsonb_agg(jsonb_build_object(
            'category', failures.quality_gate_detail,
            'count', failures.failed_count,
            'pct_of_failures', CASE WHEN totals.total_failed > 0
                THEN failures.failed_count::float / totals.total_failed ELSE 0 END
        ) ORDER BY failures.failed_count DESC), '[]'::jsonb) AS rows
    FROM (
        SELECT tenant_id, quality_gate_detail, COUNT(*)::int AS failed_count
        FROM eligible
        WHERE quality_gate_detail IS NOT NULL AND NOT quality_gate_passed
        GROUP BY tenant_id, quality_gate_detail
    ) failures
    JOIN totals USING (tenant_id)
    GROUP BY failures.tenant_id
),
tokens_by_model AS (
    SELECT
        tenant_id,
        COALESCE(jsonb_agg(jsonb_build_object(
            'model_name', model_name,
            'avg_tokens', avg_tokens,
            'avg_attempts', avg_attempts,
            'sample_count', sample_count
        ) ORDER BY avg_tokens DESC), '[]'::jsonb) AS rows
    FROM (
        SELECT
            tenant_id,
            COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name,
            COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0)::float AS avg_tokens,
            COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1)::float AS avg_attempts,
            COUNT(*)::int AS sample_count
        FROM eligible
        GROUP BY tenant_id, COALESCE(NULLIF(model_name, ''), delegated_to)
    ) by_model
    GROUP BY tenant_id
)
SELECT
    totals.tenant_id,
    CASE WHEN totals.total_checks > 0
        THEN totals.total_passed::float / totals.total_checks ELSE 0 END AS overall_pass_rate,
    totals.total_passed,
    totals.total_failed,
    totals.total_checks,
    totals.total_escalations AS escalation_count,
    CASE WHEN totals.total_checks > 0
        THEN totals.total_escalations::float / totals.total_checks ELSE 0 END AS escalation_rate,
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

ALTER VIEW projection_delegation_quality_gate SET (security_invoker = true);

COMMIT;
