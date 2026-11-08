-- OMN-22573: bound the failure_categories of projection_delegation_quality_gate.
--
-- The writer republishes this view's row for a tenant after every apply, as the
-- snapshot onex.snapshot.projection.delegation.quality-gate.v1. Migration 0045
-- grouped the failure list on the whole free-text quality_gate_detail. One dev
-- tenant held 2,586 distinct values (1,166,913 bytes of JSON, growing with every
-- failed run), so the snapshot passed the 1,048,576-byte bus limit and the
-- writer quarantined every event it read to the DLQ. The consumer group
-- local.omnimarket-projections.delegation-writer.consume.v1 then sat near
-- 19,300 behind and the lab-alarm consumer_group_lag condition flapped.
--
-- The list is now bounded by construction, not by a limit someone must raise:
--   * a category is the first 120 characters of the detail, so details that
--     differ only in a run-specific tail are one family;
--   * the 50 largest families are listed, ties broken by the text;
--   * every other family is folded into one 'other failure categories' row,
--     so the counts still add up to the failed rows that carry a detail;
--   * pct_of_failures keeps total_failed as its denominator, as in 0045.
-- A tenant's list is at most 51 entries of at most 120 characters of text.
--
-- The eligible CTE selects the columns the view reads instead of every column.
-- The CTE is read several times, so Postgres materialises it, and with
-- SELECT * that copied prompt_text and response_text for every row on every
-- republish. Measured on the dev database: the view read took 0.98 s before and
-- 0.17 s after, and the largest tenant's failure_categories fell from
-- 1,166,913 to 8,603 bytes.
--
-- Rollback: rollback/0065_quality_gate_failure_categories_bounded.sql.
BEGIN;

CREATE OR REPLACE VIEW projection_delegation_quality_gate AS
WITH eligible AS (
    SELECT
        tenant_id,
        quality_gate_passed,
        quality_gate_detail,
        escalation_count,
        tokens_to_compliance,
        compliance_attempts,
        actual_score,
        required_bar,
        created_at,
        model_name,
        delegated_to
    FROM delegation_events
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
failure_families AS (
    SELECT
        tenant_id,
        LEFT(quality_gate_detail, 120) AS category,
        COUNT(*)::int AS failed_count,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id
            ORDER BY COUNT(*) DESC, LEFT(quality_gate_detail, 120)
        ) AS family_rank
    FROM eligible
    WHERE quality_gate_detail IS NOT NULL AND NOT quality_gate_passed
    GROUP BY tenant_id, LEFT(quality_gate_detail, 120)
),
failure_categories AS (
    SELECT
        bounded.tenant_id,
        COALESCE(jsonb_agg(jsonb_build_object(
            'category', bounded.category,
            'count', bounded.failed_count,
            'pct_of_failures', CASE WHEN totals.total_failed > 0
                THEN bounded.failed_count::float / totals.total_failed ELSE 0 END
        ) ORDER BY bounded.family_rank), '[]'::jsonb) AS rows
    FROM (
        SELECT tenant_id, category, failed_count, family_rank
        FROM failure_families
        WHERE family_rank <= 50
        UNION ALL
        SELECT tenant_id, 'other failure categories', SUM(failed_count)::int, 51
        FROM failure_families
        WHERE family_rank > 50
        GROUP BY tenant_id
    ) bounded
    JOIN totals USING (tenant_id)
    GROUP BY bounded.tenant_id
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

-- CREATE OR REPLACE VIEW replaces a view's reloptions with the ones the
-- statement names; keep the invoker rights 0040 set, as 0045 does.
ALTER VIEW projection_delegation_quality_gate SET (security_invoker = true);

COMMIT;
