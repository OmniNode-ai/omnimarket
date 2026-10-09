-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-20754: the SQLite counterpart of migration 0045's
-- projection_delegation_quality_gate view, so a local store serves the
-- quality-gate exposure the Overview's Quality row reads.
--
-- Same columns, names, JSON keys and aggregates as migration 0045, with these
-- differences, each because the local store cannot express the Postgres form:
-- * Every row is eligible. Postgres drops the ('terminal_construction_failed',
--   'undetermined') outcome pair, but a local store has no operational_outcome
--   or content_verdict column, so no local row can carry that pair.
-- * median_tokens_to_compliance is computed with window functions, because
--   SQLite has no percentile_cont: the mean of the one or two middle non-zero
--   values, which is what percentile_cont(0.5) returns, and NULL with none.
-- * Ordered JSON arrays are built from ordered subqueries, because SQLite 3.40
--   (Debian bookworm's) has no ORDER BY inside an aggregate, and equal
--   counts are ordered by their name so the order is stable.
-- * provisioned is 1 where Postgres has TRUE: a SQLite column has no boolean.
--
-- Applied once per local store by SqliteDatabaseAdapter as the store step
-- omn20754_delegation_routing_quality_views; a revision is a new step, never an
-- edit here.
CREATE VIEW projection_delegation_quality_gate AS
WITH eligible AS (
    SELECT * FROM delegation_events
),
totals AS (
    SELECT
        tenant_id,
        COALESCE(SUM(CASE WHEN quality_gate_passed IS NOT NULL THEN 1 ELSE 0 END), 0)
            AS total_checks,
        COALESCE(SUM(CASE WHEN quality_gate_passed = 1 THEN 1 ELSE 0 END), 0) AS total_passed,
        COALESCE(SUM(CASE WHEN quality_gate_passed = 0 THEN 1 ELSE 0 END), 0) AS total_failed,
        COALESCE(SUM(escalation_count), 0) AS total_escalations,
        COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0.0) AS avg_tokens_to_compliance,
        COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1.0) AS avg_compliance_attempts,
        COALESCE(AVG(actual_score), 0.0) AS avg_actual_score,
        COALESCE(AVG(required_bar), 0.0) AS avg_required_bar,
        MAX(created_at) AS latest_projection_updated_at
    FROM eligible
    GROUP BY tenant_id
),
median_tokens AS (
    SELECT tenant_id, AVG(tokens) AS median_tokens_to_compliance
    FROM (
        SELECT
            tenant_id,
            tokens_to_compliance AS tokens,
            row_number() OVER (
                PARTITION BY tenant_id ORDER BY tokens_to_compliance
            ) AS position,
            COUNT(*) OVER (PARTITION BY tenant_id) AS samples
        FROM eligible
        WHERE NULLIF(tokens_to_compliance, 0) IS NOT NULL
    )
    WHERE position IN ((samples + 1) / 2, (samples + 2) / 2)
    GROUP BY tenant_id
),
failure_categories AS (
    SELECT tenant_id, json_group_array(json(entry)) AS rows
    FROM (
        SELECT
            failures.tenant_id,
            json_object(
                'category', failures.quality_gate_detail,
                'count', failures.failed_count,
                'pct_of_failures', CASE WHEN totals.total_failed > 0
                    THEN CAST(failures.failed_count AS REAL) / totals.total_failed
                    ELSE 0.0 END
            ) AS entry
        FROM (
            SELECT tenant_id, quality_gate_detail, COUNT(*) AS failed_count
            FROM eligible
            WHERE quality_gate_detail IS NOT NULL AND quality_gate_passed = 0
            GROUP BY tenant_id, quality_gate_detail
        ) AS failures
        JOIN totals ON totals.tenant_id IS failures.tenant_id
        ORDER BY failures.tenant_id, failures.failed_count DESC,
                 failures.quality_gate_detail
    )
    GROUP BY tenant_id
),
tokens_by_model AS (
    SELECT tenant_id, json_group_array(json(entry)) AS rows
    FROM (
        SELECT
            tenant_id,
            avg_tokens,
            model_name,
            json_object(
                'model_name', model_name,
                'avg_tokens', avg_tokens,
                'avg_attempts', avg_attempts,
                'sample_count', sample_count
            ) AS entry
        FROM (
            SELECT
                tenant_id,
                COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name,
                COALESCE(AVG(NULLIF(tokens_to_compliance, 0)), 0.0) AS avg_tokens,
                COALESCE(AVG(NULLIF(compliance_attempts, 0)), 1.0) AS avg_attempts,
                COUNT(*) AS sample_count
            FROM eligible
            GROUP BY tenant_id, COALESCE(NULLIF(model_name, ''), delegated_to)
        )
        ORDER BY tenant_id, avg_tokens DESC, model_name
    )
    GROUP BY tenant_id
)
SELECT
    totals.tenant_id AS tenant_id,
    CASE WHEN totals.total_checks > 0
        THEN CAST(totals.total_passed AS REAL) / totals.total_checks ELSE 0.0
    END AS overall_pass_rate,
    totals.total_passed AS total_passed,
    totals.total_failed AS total_failed,
    totals.total_checks AS total_checks,
    totals.total_escalations AS escalation_count,
    CASE WHEN totals.total_checks > 0
        THEN CAST(totals.total_escalations AS REAL) / totals.total_checks ELSE 0.0
    END AS escalation_rate,
    json_array(json_object(
        'check_type', 'score_vs_required_bar',
        'passed', totals.total_passed,
        'failed', totals.total_failed,
        'total', totals.total_checks,
        'pass_rate', CASE WHEN totals.total_checks > 0
            THEN CAST(totals.total_passed AS REAL) / totals.total_checks ELSE 0.0 END
    )) AS by_check_type,
    COALESCE(failure_categories.rows, '[]') AS failure_categories,
    totals.avg_tokens_to_compliance AS avg_tokens_to_compliance,
    median_tokens.median_tokens_to_compliance AS median_tokens_to_compliance,
    totals.avg_compliance_attempts AS avg_compliance_attempts,
    COALESCE(tokens_by_model.rows, '[]') AS tokens_to_compliance_by_model,
    COALESCE(totals.latest_projection_updated_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        AS captured_at,
    1 AS provisioned,
    totals.latest_projection_updated_at AS latest_projection_updated_at,
    totals.avg_actual_score AS avg_actual_score,
    totals.avg_required_bar AS avg_required_bar
FROM totals
LEFT JOIN median_tokens ON median_tokens.tenant_id IS totals.tenant_id
LEFT JOIN failure_categories ON failure_categories.tenant_id IS totals.tenant_id
LEFT JOIN tokens_by_model ON tokens_by_model.tenant_id IS totals.tenant_id;
