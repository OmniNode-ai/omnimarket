-- OMN-15533: stop the delegation-savings read views fabricating task_type,
-- token counts, and savings provenance.
--
-- Three defects, all on the `savings_sessions` CTE (the ONLY branch that produces
-- rows on the dev lane, because delegation_events is empty there — OMN-14153):
--
--   1. `model_local AS task_type` overwrote the task class with the MODEL NAME.
--      A row whose source terminal carried task_type=escalation rendered
--      task_type="gemini-2.5-flash". Repeated verbatim in 076, 078 and 079.
--   2. `0::int AS prompt_tokens` / `0::int AS completion_tokens` hardcoded zero
--      on every row while the source terminals carried real counts.
--   3. `'measured' AS savings_method` stamped a provenance claim on rows whose own
--      pricing_manifest_version is 'savings-estimated' and whose tokens were the
--      hardcoded 0 above — a false measurement claim of the same class as
--      OMN-13355, re-introduced in the read view.
--
-- WHY A NEW MIGRATION RATHER THAN AN EDIT TO 076/078/079
-- ------------------------------------------------------
-- scripts/run-projection-migrations.py records a sha256 per applied migration and
-- raises "Checksum mismatch for already-applied migration ... Schema drift
-- detected -- manual intervention required" when a file's bytes change after it
-- has run. Editing 076, 078 or 079 in place would therefore hard-fail the runner
-- on every database that has already migrated, including the dev lane. Both views
-- are CREATE OR REPLACE and hold no data, so replacing them forward is the only
-- non-breaking way to apply the fix to all three view definitions.
--
-- CREATE OR REPLACE VIEW cannot rename, retype or reorder an existing output
-- column -- it can only append. Every pre-existing output column of both views is
-- therefore reproduced here in its original position and type; the only structural
-- change is the appended cumulative_counterfactual_baseline_usd (see AC4 below).
--
-- PROVENANCE RULE (AC3)
-- ---------------------
-- savings_method / usage_source are now derived from the persisted token counts on
-- BOTH branches, using the same test the event branch already applied:
--     served tokens > 0  -> 'measured'
--     otherwise          -> 'estimated' / 'unknown'
-- A row can no longer be labelled 'measured' while carrying zero-or-unrecorded
-- tokens, so the falsifying conjunction in AC3 is unreachable by construction.
-- usage_source on the savings branch previously emitted the literal
-- 'savings_estimates', which is not one of the three values the consumer contract
-- (omnidash delegation-savings.types.ts) declares; it now emits 'measured' /
-- 'unknown' like the event branch.
--
-- COUNTERFACTUAL vs ACTUAL COST (AC4 -- partial, see PR body)
-- -----------------------------------------------------------
-- cumulative_cloud_cost_usd does NOT hold cloud spend: it holds the pinned premium
-- counterfactual (the dev-lane 4.124505 is what claude-opus-4-6 WOULD have cost,
-- not money spent). This migration appends counterfactual_baseline_usd per session
-- and cumulative_counterfactual_baseline_usd in the totals, so the counterfactual
-- is available under a name that says what it is. The legacy *_cloud_cost_usd
-- columns are retained unchanged and carry the same value, because Postgres cannot
-- rename a view column in place and four omnidash call sites still read them.
-- Attributing genuine ACTUAL cloud spend needs cost_tier_name, which exists only on
-- delegation_events -- empty on the dev lane and out of scope on this ticket -- so
-- it is deliberately NOT synthesised here rather than invented.

CREATE OR REPLACE VIEW public.projection_delegation_savings AS
WITH savings_sessions AS (
    SELECT
        session_id,
        -- OMN-15533 (1): the real task class, never model_local. NULL (row
        -- predates migration 082) collapses to '' -- an absent class, not a
        -- model name.
        COALESCE(task_type, '') AS task_type,
        model_local AS model_name,
        local_cost_usd::float AS local_cost_usd,
        cloud_cost_usd::float AS cloud_cost_usd,
        -- OMN-15533 (AC4): same value as cloud_cost_usd, under the name that
        -- describes it. This is a pinned counterfactual, not spend.
        cloud_cost_usd::float AS counterfactual_baseline_usd,
        savings_usd::float AS savings_usd,
        model_cloud_baseline AS baseline_model,
        'savings-estimated' AS pricing_manifest_version,
        -- OMN-15533 (3): provenance derived from the persisted tokens, never a
        -- hardcoded literal.
        CASE WHEN COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) > 0
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE WHEN COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) > 0
            THEN 'measured' ELSE 'unknown' END AS usage_source,
        -- OMN-15533 (2): the real served token counts (migration 082). NULL is
        -- preserved as NULL -- "not recorded" is not the same claim as zero.
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
        -- Aligned with the savings branch: a saving is only 'measured' when real
        -- served tokens back it. cost_savings_usd > 0 alone proved nothing about
        -- how the number was obtained.
        CASE WHEN COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0) > 0
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE WHEN COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0) > 0
            THEN 'measured' ELSE 'unknown' END AS usage_source,
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
    -- Appended (CREATE OR REPLACE permits append only). Same value as
    -- cumulative_cloud_cost_usd, named for what it actually is.
    totals.cumulative_counterfactual_baseline_usd
FROM totals
CROSS JOIN sessions
LEFT JOIN latest ON TRUE;


-- The series view carries the SAME three defects in its own copy of the
-- savings_sessions CTE (078:21 / 079:35). Its eight output columns are unchanged
-- and reproduced in their original order and type; only the CTE is corrected.
-- The tier-mix machinery from 079 (event_tiers, classified_sessions, the
-- local/cheap/premium mapping and the tier-routed denominator) is preserved
-- verbatim -- this migration does not re-litigate OMN-13661.
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
        'savings-estimated' AS pricing_manifest_version,
        CASE WHEN COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) > 0
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE WHEN COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) > 0
            THEN 'measured' ELSE 'unknown' END AS usage_source,
        prompt_tokens::int AS prompt_tokens,
        completion_tokens::int AS completion_tokens,
        NULL::int AS tokens_to_compliance,
        NULL::int AS latency_ms,
        created_at,
        NULL::text AS prompt_text,
        NULL::text AS response_text,
        -- savings_estimates rows are not LLM-tier-routed: no serving tier.
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
        CASE WHEN COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0) > 0
            THEN 'measured' ELSE 'estimated' END AS savings_method,
        CASE WHEN COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0) > 0
            THEN 'measured' ELSE 'unknown' END AS usage_source,
        COALESCE(tokens_input, 0)::int AS prompt_tokens,
        COALESCE(tokens_output, 0)::int AS completion_tokens,
        NULLIF(tokens_to_compliance, 0)::int AS tokens_to_compliance,
        COALESCE(delegation_latency_ms, latency_ms)::int AS latency_ms,
        COALESCE(created_at, timestamp) AS created_at,
        prompt_text,
        response_text,
        -- OMN-13649: authoritative serving tier. Empty string -> not tier-routed.
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
            ELSE NULL  -- not_tier_routed: excluded from the tier-% denominator
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
