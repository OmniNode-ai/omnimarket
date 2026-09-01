-- OMN-15533 (AC3, second pass): both delegation-savings views read the provenance
-- their SOURCE ROW recorded, instead of inferring one from token counts.
--
-- WHAT MIGRATION 083 GOT WRONG
-- ----------------------------
-- 083 correctly deleted the hardcoded `'measured' AS savings_method` literal, but
-- replaced it with:
--
--     CASE WHEN COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) > 0
--         THEN 'measured' ELSE 'estimated' END
--
-- Served tokens are a fact about the REQUEST; they say nothing about how the
-- SAVING was obtained. The consequence is a live false claim in the one direction
-- that matters: a row whose saving came from ModelSavingsEstimate's
-- `direct_savings_usd + heuristic_savings_usd` -- an estimate, and one the producer
-- labels `is_measured=false` / `usage_source='ESTIMATED'` on the wire -- renders
-- `savings_method='measured'` the moment it carries any token count at all.
--
-- It also made AC3 unfalsifiable rather than true: `measured` was tied to the
-- exact negation of the AC's "and tokens are 0" clause, so the conjunction became
-- unreachable by construction. This migration makes it reachable again and makes
-- it resolve honestly -- an estimate-derived row keeps `pricing_manifest_version`
-- = 'savings-estimated' AND carries real tokens AND still reads `estimated`.
--
-- THE RULE
-- --------
--   savings branch  ->  savings_estimates.savings_method / .usage_source /
--                       .pricing_manifest_version (migration 085), which the write
--                       path fills from the source event's own is_measured /
--                       usage_source / pricing_manifest_version.
--   event branch    ->  delegation_events.cost_measurement_source (migration 0018),
--                       which records HOW cost_usd was derived. A saving is
--                       measured when the actual cost underneath it was measured
--                       (metered / free_local / budgeted_*), and estimated when
--                       that cost was itself modelled from a manifest
--                       (manifest_compute). This does not re-litigate OMN-13629's
--                       position that a counterfactual re-derived from really-served
--                       tokens yields a measurement -- it only stops the OTHER half
--                       of the subtraction being assumed.
--
-- UNRECORDED PROVENANCE IS A REFUSAL, NOT A DEFAULT
-- -------------------------------------------------
-- NULL / '' provenance renders `savings_method='estimated'` and
-- `usage_source='unknown'`. `unknown` is the consumer contract's own refusal value
-- (omnidash delegation-savings.types.ts: usage_source is
-- 'measured' | 'estimated' | 'unknown'); savings_method has no third value there,
-- so it floors at `estimated` -- the conservative direction, because claiming a
-- measurement requires the source to have claimed one.
--
-- The `'savings-estimated'` pricing_manifest_version fallback is retained
-- DELIBERATELY for rows that recorded no manifest. Replacing it would once again
-- put AC3's falsifying conjunction out of reach; keeping it means the falsifier
-- stays firable and is proven to resolve honestly. Rows whose producer DID send a
-- manifest version now carry that real value instead.
--
-- WHY A NEW MIGRATION RATHER THAN AN EDIT TO 083
-- ----------------------------------------------
-- scripts/run-projection-migrations.py records a sha256 per applied migration and
-- hard-fails with "Checksum mismatch for already-applied migration ... Schema
-- drift detected" when a file's bytes change after it has run. 083 is applied on
-- the dev lane. Both views are CREATE OR REPLACE and hold no data, so replacing
-- them forward is the only non-breaking way to correct them.
--
-- CREATE OR REPLACE VIEW cannot rename, retype or reorder an existing output
-- column. No column is added, removed or moved here; only the three provenance
-- expressions change on each branch. Every other line is 083's, reproduced
-- verbatim so the diff is exactly the provenance fix.

CREATE OR REPLACE VIEW public.projection_delegation_savings AS
WITH savings_sessions AS (
    SELECT
        session_id,
        COALESCE(task_type, '') AS task_type,
        model_local AS model_name,
        local_cost_usd::float AS local_cost_usd,
        cloud_cost_usd::float AS cloud_cost_usd,
        cloud_cost_usd::float AS counterfactual_baseline_usd,
        savings_usd::float AS savings_usd,
        model_cloud_baseline AS baseline_model,
        -- OMN-15533: the manifest the source actually priced with. The legacy
        -- literal remains the fallback for rows that recorded none (see header).
        COALESCE(pricing_manifest_version, 'savings-estimated')
            AS pricing_manifest_version,
        -- OMN-15533: the source's own provenance. Never inferred from tokens.
        COALESCE(savings_method, 'estimated') AS savings_method,
        COALESCE(usage_source, 'unknown') AS usage_source,
        prompt_tokens::int AS prompt_tokens,
        completion_tokens::int AS completion_tokens,
        NULL::int AS tokens_to_compliance,
        NULL::int AS latency_ms,
        created_at,
        NULL::text AS prompt_text,
        NULL::text AS response_text
    FROM public.savings_estimates
),
event_sessions AS (
    SELECT
        COALESCE(NULLIF(session_id, ''), NULLIF(correlation_id, ''), id::text)
            AS session_id,
        COALESCE(task_type, '') AS task_type,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'local')
            AS model_name,
        COALESCE(cost_usd, 0)::float AS local_cost_usd,
        (COALESCE(cost_usd, 0) + COALESCE(cost_savings_usd, 0))::float
            AS cloud_cost_usd,
        (COALESCE(cost_usd, 0) + COALESCE(cost_savings_usd, 0))::float
            AS counterfactual_baseline_usd,
        COALESCE(cost_savings_usd, 0)::float AS savings_usd,
        'claude-opus-4.1' AS baseline_model,
        pricing_manifest_version::text AS pricing_manifest_version,
        -- OMN-15533: derived from how the ACTUAL cost was obtained (0018), not
        -- from token presence.
        CASE WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split')
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE
            WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split') THEN 'measured'
            WHEN cost_measurement_source = 'manifest_compute' THEN 'estimated'
            ELSE 'unknown'
        END AS usage_source,
        COALESCE(tokens_input, 0)::int AS prompt_tokens,
        COALESCE(tokens_output, 0)::int AS completion_tokens,
        NULLIF(tokens_to_compliance, 0)::int AS tokens_to_compliance,
        COALESCE(delegation_latency_ms, latency_ms)::int AS latency_ms,
        COALESCE(created_at, timestamp) AS created_at,
        prompt_text,
        response_text
    FROM public.delegation_events
),
combined_sessions AS (
    SELECT * FROM savings_sessions
    UNION ALL
    SELECT event_sessions.*
    FROM event_sessions
    WHERE NOT EXISTS (
        SELECT 1
        FROM savings_sessions
        WHERE savings_sessions.session_id = event_sessions.session_id
    )
),
limited_sessions AS (
    SELECT *
    FROM combined_sessions
    ORDER BY created_at DESC
    LIMIT 500
),
totals AS (
    SELECT
        COALESCE(SUM(savings_usd), 0)::float AS cumulative_savings_usd,
        COALESCE(SUM(local_cost_usd), 0)::float AS cumulative_local_cost_usd,
        COALESCE(SUM(cloud_cost_usd), 0)::float AS cumulative_cloud_cost_usd,
        COALESCE(SUM(counterfactual_baseline_usd), 0)::float
            AS cumulative_counterfactual_baseline_usd,
        COUNT(*)::int AS session_count,
        MAX(created_at) AS latest_projection_updated_at
    FROM combined_sessions
),
sessions AS (
    SELECT COALESCE(
        jsonb_agg(to_jsonb(limited_sessions) ORDER BY created_at DESC),
        '[]'::jsonb
    ) AS rows
    FROM limited_sessions
),
latest AS (
    SELECT
        baseline_model,
        pricing_manifest_version
    FROM combined_sessions
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    totals.cumulative_savings_usd,
    totals.cumulative_local_cost_usd,
    totals.cumulative_cloud_cost_usd,
    COALESCE(latest.baseline_model, 'claude-opus-4.1') AS baseline_model,
    COALESCE(latest.pricing_manifest_version, 'runtime-delegation-events')
        AS pricing_manifest_version,
    totals.session_count,
    sessions.rows AS sessions,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at,
    totals.cumulative_counterfactual_baseline_usd
FROM totals
CROSS JOIN sessions
LEFT JOIN latest ON TRUE;


-- The series view carries the same token-inferred provenance in its own copy of
-- both CTEs (083:204 / 083:232). Its eight output columns are unchanged and
-- reproduced in their original order and type; the tier-mix machinery from 079 is
-- preserved verbatim.
CREATE OR REPLACE VIEW public.projection_delegation_savings_series AS
WITH savings_sessions AS (
    SELECT
        session_id,
        COALESCE(task_type, '') AS task_type,
        model_local AS model_name,
        local_cost_usd::float AS local_cost_usd,
        cloud_cost_usd::float AS cloud_cost_usd,
        savings_usd::float AS savings_usd,
        model_cloud_baseline AS baseline_model,
        COALESCE(pricing_manifest_version, 'savings-estimated')
            AS pricing_manifest_version,
        COALESCE(savings_method, 'estimated') AS savings_method,
        COALESCE(usage_source, 'unknown') AS usage_source,
        prompt_tokens::int AS prompt_tokens,
        completion_tokens::int AS completion_tokens,
        NULL::int AS tokens_to_compliance,
        NULL::int AS latency_ms,
        created_at,
        NULL::text AS prompt_text,
        NULL::text AS response_text,
        NULL::text AS cost_tier_name
    FROM public.savings_estimates
),
event_sessions AS (
    SELECT
        COALESCE(NULLIF(session_id, ''), NULLIF(correlation_id, ''), id::text)
            AS session_id,
        COALESCE(task_type, '') AS task_type,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'local')
            AS model_name,
        COALESCE(cost_usd, 0)::float AS local_cost_usd,
        (COALESCE(cost_usd, 0) + COALESCE(cost_savings_usd, 0))::float
            AS cloud_cost_usd,
        COALESCE(cost_savings_usd, 0)::float AS savings_usd,
        'claude-opus-4.1' AS baseline_model,
        pricing_manifest_version::text AS pricing_manifest_version,
        CASE WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split')
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE
            WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split') THEN 'measured'
            WHEN cost_measurement_source = 'manifest_compute' THEN 'estimated'
            ELSE 'unknown'
        END AS usage_source,
        COALESCE(tokens_input, 0)::int AS prompt_tokens,
        COALESCE(tokens_output, 0)::int AS completion_tokens,
        NULLIF(tokens_to_compliance, 0)::int AS tokens_to_compliance,
        COALESCE(delegation_latency_ms, latency_ms)::int AS latency_ms,
        COALESCE(created_at, timestamp) AS created_at,
        prompt_text,
        response_text,
        NULLIF(cost_tier_name, '') AS cost_tier_name
    FROM public.delegation_events
),
event_tiers AS (
    SELECT
        session_id,
        (array_agg(cost_tier_name ORDER BY created_at DESC)
            FILTER (WHERE cost_tier_name IS NOT NULL))[1] AS cost_tier_name
    FROM event_sessions
    GROUP BY session_id
),
combined_sessions AS (
    SELECT
        savings_sessions.session_id,
        savings_sessions.task_type,
        savings_sessions.model_name,
        savings_sessions.local_cost_usd,
        savings_sessions.cloud_cost_usd,
        savings_sessions.savings_usd,
        savings_sessions.baseline_model,
        savings_sessions.pricing_manifest_version,
        savings_sessions.savings_method,
        savings_sessions.usage_source,
        savings_sessions.prompt_tokens,
        savings_sessions.completion_tokens,
        savings_sessions.tokens_to_compliance,
        savings_sessions.latency_ms,
        savings_sessions.created_at,
        savings_sessions.prompt_text,
        savings_sessions.response_text,
        COALESCE(event_tiers.cost_tier_name, savings_sessions.cost_tier_name)
            AS cost_tier_name
    FROM savings_sessions
    LEFT JOIN event_tiers USING (session_id)
    UNION ALL
    SELECT event_sessions.*
    FROM event_sessions
    WHERE NOT EXISTS (
        SELECT 1
        FROM savings_sessions
        WHERE savings_sessions.session_id = event_sessions.session_id
    )
),
classified_sessions AS (
    SELECT
        *,
        CASE
            WHEN cost_tier_name = 'local' THEN 'local'
            WHEN cost_tier_name IN ('cheap_cloud', 'cheap_frontier') THEN 'cheap'
            WHEN cost_tier_name = 'claude' THEN 'premium'
            ELSE NULL
        END AS tier_bucket
    FROM combined_sessions
)
SELECT
    date_trunc('day', created_at) AS bucket,
    COALESCE(SUM(local_cost_usd), 0)::float AS actual_cost_usd,
    COALESCE(SUM(cloud_cost_usd), 0)::float AS baseline_cost_usd,
    COALESCE(SUM(savings_usd), 0)::float AS savings_usd,
    COUNT(*)::int AS task_count,
    COALESCE(
        COUNT(*) FILTER (WHERE tier_bucket = 'local')::float
        / NULLIF(COUNT(*) FILTER (WHERE tier_bucket IS NOT NULL), 0),
        0
    )::float AS local_pct,
    COALESCE(
        COUNT(*) FILTER (WHERE tier_bucket = 'cheap')::float
        / NULLIF(COUNT(*) FILTER (WHERE tier_bucket IS NOT NULL), 0),
        0
    )::float AS cheap_pct,
    COALESCE(
        COUNT(*) FILTER (WHERE tier_bucket = 'premium')::float
        / NULLIF(COUNT(*) FILTER (WHERE tier_bucket IS NOT NULL), 0),
        0
    )::float AS prem_pct
FROM classified_sessions
GROUP BY 1
ORDER BY 1;
