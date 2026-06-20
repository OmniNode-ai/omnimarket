-- OMN-13368: persist score-vs-required-bar authority evidence on delegation rows.
--
-- Delegation escalation now routes on actual_score < required_bar. These columns
-- make the decision materialized and queryable through the contract-declared
-- projection API instead of hiding it in logs or client state.

ALTER TABLE delegation_events
    ADD COLUMN IF NOT EXISTS required_bar NUMERIC(5, 3),
    ADD COLUMN IF NOT EXISTS actual_score NUMERIC(5, 3),
    ADD COLUMN IF NOT EXISTS escalation_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS authority_source TEXT,
    ADD COLUMN IF NOT EXISTS score_source TEXT,
    ADD COLUMN IF NOT EXISTS request_override_applied BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS override_within_bounds BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_delegation_events_score_vs_bar
    ON delegation_events (required_bar, actual_score);

CREATE OR REPLACE VIEW projection_delegation_quality_gate AS
WITH totals AS (
    SELECT
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
    totals.total_escalations AS escalation_count,
    CASE WHEN totals.total_checks > 0
        THEN totals.total_escalations::float / totals.total_checks ELSE 0 END
        AS escalation_rate,
    totals.avg_actual_score,
    totals.avg_required_bar,
    jsonb_build_array(jsonb_build_object(
        'check_type', 'score_vs_required_bar',
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
