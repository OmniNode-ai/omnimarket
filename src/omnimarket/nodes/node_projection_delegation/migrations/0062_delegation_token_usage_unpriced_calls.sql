-- OMN-20717: the token-usage aggregate counts unpriced calls instead of
-- pricing them at zero.
--
-- 0061 lets delegation_events.cost_usd be NULL for a call no cost model priced.
-- 0039's projection_delegation_token_usage summed it as COALESCE(SUM(cost_usd),
-- 0), which turns "not priced" back into "cost nothing". Ruling (2026-10-14): a
-- sum of cost_usd treats NULL as unknown, not zero, and shows how many calls
-- were unpriced next to the total.
--
-- So, per model and per tenant:
--   * estimated_cost_usd / total_estimated_cost_usd = SUM(cost_usd): the priced
--     calls only, NULL when none of them was priced. A free local call is a
--     priced zero and still sums as 0.
--   * unpriced_call_count = the calls whose cost_usd is NULL.
-- Everything else is byte-for-byte 0039's definition. The view is dropped and
-- re-created because its output columns change, and both reader grants are
-- re-issued for the reason 0039 states at its foot.

BEGIN;

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
        SUM(cost_usd)::float AS estimated_cost_usd,
        COUNT(*) FILTER (WHERE cost_usd IS NULL)::int AS unpriced_call_count,
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
        SUM(cost_usd)::float AS total_estimated_cost_usd,
        COUNT(*) FILTER (WHERE cost_usd IS NULL)::int AS unpriced_call_count,
        MAX(created_at) AS latest_projection_updated_at
    FROM delegation_events
    GROUP BY tenant_id
),
by_model AS (
    SELECT
        tenant_id,
        COALESCE(
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
    totals.unpriced_call_count,
    provenance.summary AS provenance_summary,
    COALESCE(by_model.rows, '[]'::jsonb) AS by_model,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at
FROM totals
LEFT JOIN provenance USING (tenant_id)
LEFT JOIN by_model USING (tenant_id);

-- The DROP above discards the view's options as well as its grants. 0040 set
-- security_invoker so the tenant policy on delegation_events applies to the
-- reader, not to the view's owner; restate it, or a superuser-owned copy reads
-- every tenant's rows.
ALTER VIEW projection_delegation_token_usage SET (security_invoker = true);

GRANT SELECT ON projection_delegation_token_usage TO app_dashboard;
GRANT SELECT ON projection_delegation_token_usage TO tenant_projection_writer;

COMMIT;
