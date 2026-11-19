-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-24615 AC6 (the 2026-11-19 re-review of #6442): the revision of delegation_quality_gate_view_v2.sql. One
-- change, nothing else: each tokens_to_compliance_by_model row also serves "backends", the sorted aliases its runs
-- were reached through -- delegated_to where it is set and differs from the model that ran, as the metering record
-- defines its backend. The view's columns are unchanged (the contract fixes them; the list rides in the JSON).
--
-- Applied once per local store by SqliteDatabaseAdapter as the store step omn24615_quality_gate_backends;
-- a revision is a new step, never an edit here.
CREATE VIEW projection_delegation_quality_gate AS
WITH eligible AS (
    SELECT * FROM delegation_events WHERE data_source IS NOT 'experiment'
),
totals AS (
    SELECT
        tenant_id,
        COALESCE(SUM(CASE WHEN (quality_gates_checked IS NOT 0 AND quality_gate_passed IS NOT NULL) THEN 1 ELSE 0 END), 0)
            AS total_checks,
        COALESCE(SUM(CASE WHEN quality_gates_checked IS NOT 0 AND quality_gate_passed = 1 THEN 1 ELSE 0 END), 0)
            AS total_passed,
        COALESCE(SUM(CASE WHEN quality_gates_checked IS NOT 0 AND quality_gate_passed = 0 THEN 1 ELSE 0 END), 0)
            AS total_failed,
        COALESCE(SUM(CASE WHEN quality_gates_checked = 0 THEN 1 ELSE 0 END), 0) AS not_evaluated,
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
              AND quality_gates_checked IS NOT 0
            GROUP BY tenant_id, quality_gate_detail
        ) AS failures
        JOIN totals ON totals.tenant_id IS failures.tenant_id
        ORDER BY failures.tenant_id, failures.failed_count DESC,
                 failures.quality_gate_detail
    )
    GROUP BY tenant_id
),
model_backends AS (
    SELECT tenant_id, model_name AS model_name, json_group_array(backend) AS backends
    FROM (
        SELECT DISTINCT tenant_id, COALESCE(NULLIF(model_name, ''), delegated_to) AS model_name, delegated_to AS backend
        FROM eligible
        WHERE COALESCE(delegated_to, '') <> ''
            AND delegated_to IS NOT COALESCE(NULLIF(model_name, ''), delegated_to)
        ORDER BY 1, 2, 3
    )
    GROUP BY tenant_id, model_name
),
tokens_by_model AS (
    SELECT tenant_id, json_group_array(json(entry)) AS rows
    FROM (
        SELECT
            per_model.tenant_id AS tenant_id,
            per_model.avg_tokens AS avg_tokens,
            per_model.model_name AS model_name,
            json_object(
                'model_name', per_model.model_name,
                'avg_tokens', per_model.avg_tokens,
                'avg_attempts', per_model.avg_attempts,
                'sample_count', per_model.sample_count,
                'backends', json(COALESCE(model_backends.backends, '[]'))
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
        ) AS per_model
        LEFT JOIN model_backends
            ON model_backends.tenant_id IS per_model.tenant_id
            AND model_backends.model_name IS per_model.model_name
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
        'not_evaluated', totals.not_evaluated,
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
