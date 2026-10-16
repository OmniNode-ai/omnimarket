-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-20717: the delegation savings aggregate counts unpriced calls.
--
-- node_projection_delegation 0061 lets delegation_events.cost_usd be NULL for
-- a call no cost model priced. Ruling (2026-10-14): a sum of cost_usd treats
-- NULL as unknown, not zero, and shows how many calls were unpriced next to
-- the total.
--
-- This view adds such a call in as 0 (COALESCE(cost_usd, 0)), so each total
-- already equals the sum of the priced calls. What it could not do was say
-- so: a tenant whose calls were all unpriced read a local cost of 0.0 with
-- nothing beside it. This forward replacement appends unpriced_call_count.
--
-- Everything else is 090's definition byte for byte. The totals keep their
-- values and stay numbers, because the dashboards read them as numbers. The
-- sessions array is unchanged, so the size ratchet still holds. The column is
-- appended, so CREATE OR REPLACE is legal and no grant is discarded;
-- security_invoker is set again below rather than assumed.
--
-- 093 (series) and 094 (overview) are not touched: no delegation_events row
-- reaches the series, and the overview already excludes unpriced runs from its
-- totals and counts them as unknown_run_count.

CREATE OR REPLACE VIEW public.projection_delegation_savings AS
WITH raw_savings_sessions AS (
    SELECT
        -- OMN-17426: the house tenant is spelled differently by the two
        -- sources and this is where they are reconciled.
        -- `savings_estimates.tenant_id` is TEXT and stores the house SLUG
        -- 'omninode'; `delegation_events.tenant_id` is uuid (that node's
        -- migration 0034) and stores the house UUID. Left alone, one
        -- logical tenant becomes TWO groups in this view, the writer can
        -- only ever republish one of them, and the other silently never
        -- reaches the page. Worse, the writer's re-read binds the slug as
        -- `app.tenant_id` and `delegation_events`' policy casts that GUC to
        -- uuid, so the read does not return the wrong rows -- it ABORTS
        -- with `invalid input syntax for type uuid: "omninode"`, which is
        -- the same failure node_projection_delegation hit under OMN-18139.
        --
        -- The UUID is not invented here: it is the platform's own
        -- `HOUSE_TENANT_UUID`, the value `_house_tenant_interim_default`
        -- already stamps for uuid-converted tables, and the same rekey
        -- node_projection_delegation/0030 performed for delegation_budget_state.
        -- Every other tenant value is already a UUID in both columns and
        -- passes through untouched.
        CASE WHEN tenant_id = 'omninode' THEN '820272f9-4aaf-5add-a2df-0af942852ab2'
             ELSE tenant_id::text END AS tenant_id,
        session_id,
        COALESCE(task_type, '') AS task_type,
        model_local AS model_name,
        local_cost_usd::float AS local_cost_usd,
        cloud_cost_usd::float AS cloud_cost_usd,
        cloud_cost_usd::float AS counterfactual_baseline_usd,
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
        created_at::timestamptz AS created_at,
        NULL::text AS prompt_text,
        NULL::text AS response_text
    FROM public.savings_estimates
    WHERE tenant_id IS NOT NULL
      AND session_id IS NOT NULL
),
savings_sessions AS (
    SELECT
        tenant_id, session_id, task_type, model_name, local_cost_usd,
        cloud_cost_usd, counterfactual_baseline_usd, savings_usd,
        baseline_model, pricing_manifest_version, savings_method, usage_source,
        prompt_tokens, completion_tokens, tokens_to_compliance, latency_ms,
        created_at, prompt_text, response_text
    FROM (
        SELECT
            raw_savings_sessions.*,
            ROW_NUMBER() OVER (
                PARTITION BY tenant_id, session_id
                ORDER BY created_at DESC, session_id DESC
            ) AS tenant_session_rank
        FROM raw_savings_sessions
    ) ranked
    WHERE tenant_session_rank = 1
),
event_sessions AS (
    SELECT
        tenant_id::text AS tenant_id,
        COALESCE(NULLIF(correlation_id, ''), NULLIF(session_id, ''), id::text)
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
        NULL::text AS baseline_model,
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
        COALESCE(created_at, timestamp)::timestamptz AS created_at,
        prompt_text,
        response_text
    FROM public.delegation_events
    WHERE tenant_id IS NOT NULL
),
combined_sessions AS (
    SELECT
        event_sessions.tenant_id,
        event_sessions.session_id,
        event_sessions.task_type,
        event_sessions.model_name,
        COALESCE(savings_sessions.local_cost_usd, event_sessions.local_cost_usd)
            AS local_cost_usd,
        COALESCE(savings_sessions.cloud_cost_usd, event_sessions.cloud_cost_usd)
            AS cloud_cost_usd,
        COALESCE(
            savings_sessions.counterfactual_baseline_usd,
            event_sessions.counterfactual_baseline_usd
        ) AS counterfactual_baseline_usd,
        COALESCE(savings_sessions.savings_usd, event_sessions.savings_usd)
            AS savings_usd,
        COALESCE(savings_sessions.baseline_model, event_sessions.baseline_model)
            AS baseline_model,
        COALESCE(
            savings_sessions.pricing_manifest_version,
            event_sessions.pricing_manifest_version
        ) AS pricing_manifest_version,
        COALESCE(savings_sessions.savings_method, event_sessions.savings_method)
            AS savings_method,
        COALESCE(savings_sessions.usage_source, event_sessions.usage_source)
            AS usage_source,
        COALESCE(
            NULLIF(savings_sessions.prompt_tokens, 0),
            event_sessions.prompt_tokens
        ) AS prompt_tokens,
        COALESCE(
            NULLIF(savings_sessions.completion_tokens, 0),
            event_sessions.completion_tokens
        ) AS completion_tokens,
        event_sessions.tokens_to_compliance,
        event_sessions.latency_ms,
        COALESCE(
            GREATEST(event_sessions.created_at, savings_sessions.created_at),
            event_sessions.created_at,
            savings_sessions.created_at
        ) AS created_at,
        event_sessions.prompt_text,
        event_sessions.response_text
    FROM event_sessions
    LEFT JOIN savings_sessions
      ON savings_sessions.session_id = event_sessions.session_id
     AND savings_sessions.tenant_id = event_sessions.tenant_id
    UNION ALL
    SELECT savings_sessions.*
    FROM savings_sessions
    WHERE NOT EXISTS (
        SELECT 1
        FROM event_sessions
        WHERE event_sessions.session_id = savings_sessions.session_id
          AND event_sessions.tenant_id = savings_sessions.tenant_id
    )
),
ranked_sessions AS (
    SELECT
        combined_sessions.*,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id ORDER BY created_at DESC
        ) AS tenant_rank
    FROM combined_sessions
),
limited_sessions AS (
    -- OMN-18851: `prompt_text` and `response_text` are DELIBERATELY ABSENT
    -- from this select list, and this CTE is the only input to the `sessions`
    -- aggregate below. They are still carried by `combined_sessions` and
    -- `ranked_sessions`, which no aggregate reads; dropping them HERE is what
    -- keeps them out of `to_jsonb(limited_sessions)`. Migration 090 records
    -- the measured snapshot size that made this necessary.
    --
    -- The two fields are not lost. They stay on `delegation_events` and are
    -- served per correlation by the projection API's `?correlation_id=`
    -- route, which is the canonical render surface for them; no dashboard
    -- component reads either field off this sessions array. An unbounded
    -- artifact belongs behind a reference, not inside a snapshot that is
    -- republished on every event -- the same call
    -- `sqlite_metering_reader.py` already makes for the same two columns.
    SELECT
        tenant_id, session_id, task_type, model_name, local_cost_usd,
        cloud_cost_usd, counterfactual_baseline_usd, savings_usd,
        baseline_model, pricing_manifest_version, savings_method, usage_source,
        prompt_tokens, completion_tokens, tokens_to_compliance, latency_ms,
        created_at
    FROM ranked_sessions
    WHERE tenant_rank <= 500
),
totals AS (
    SELECT
        tenant_id,
        COALESCE(SUM(savings_usd), 0)::float AS cumulative_savings_usd,
        COALESCE(SUM(local_cost_usd), 0)::float AS cumulative_local_cost_usd,
        COALESCE(SUM(cloud_cost_usd), 0)::float AS cumulative_cloud_cost_usd,
        COALESCE(SUM(counterfactual_baseline_usd), 0)::float
            AS cumulative_counterfactual_baseline_usd,
        COUNT(*)::int AS session_count,
        MAX(created_at) AS latest_projection_updated_at
    FROM combined_sessions
    GROUP BY tenant_id
),
sessions AS (
    -- `- 'tenant_id'` keeps the element shape byte-identical to what consumers
    -- read today: the tenant is the ROW's identity, not a field of each
    -- session, and every element of this array belongs to the row's tenant by
    -- construction.
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(
                to_jsonb(limited_sessions) - 'tenant_id' ORDER BY created_at DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM limited_sessions
    GROUP BY tenant_id
),
latest AS (
    SELECT tenant_id, baseline_model, pricing_manifest_version
    FROM (
        SELECT
            tenant_id,
            baseline_model,
            pricing_manifest_version,
            ROW_NUMBER() OVER (
                PARTITION BY tenant_id ORDER BY created_at DESC
            ) AS tenant_rank
        FROM combined_sessions
    ) ranked
    WHERE tenant_rank = 1
),
unpriced AS (
    -- A delegation whose cost no cost model priced, and that no savings
    -- estimate prices either. combined_sessions has already turned its NULL
    -- into 0, so the count is taken from the stored column.
    SELECT
        delegation_events.tenant_id::text AS tenant_id,
        COUNT(*)::int AS unpriced_call_count
    FROM public.delegation_events
    WHERE delegation_events.tenant_id IS NOT NULL
      AND delegation_events.cost_usd IS NULL
      AND NOT EXISTS (
          SELECT 1
          FROM savings_sessions
          WHERE savings_sessions.tenant_id = delegation_events.tenant_id::text
            AND savings_sessions.session_id = COALESCE(
                NULLIF(delegation_events.correlation_id, ''),
                NULLIF(delegation_events.session_id, ''),
                delegation_events.id::text
            )
            AND savings_sessions.local_cost_usd IS NOT NULL
      )
    GROUP BY delegation_events.tenant_id
)
SELECT
    totals.cumulative_savings_usd,
    totals.cumulative_local_cost_usd,
    totals.cumulative_cloud_cost_usd,
    latest.baseline_model AS baseline_model,
    COALESCE(latest.pricing_manifest_version, 'runtime-delegation-events')
        AS pricing_manifest_version,
    totals.session_count,
    COALESCE(sessions.rows, '[]'::jsonb) AS sessions,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at,
    totals.cumulative_counterfactual_baseline_usd,
    totals.tenant_id,
    COALESCE(unpriced.unpriced_call_count, 0)::int AS unpriced_call_count
FROM totals
LEFT JOIN sessions ON sessions.tenant_id = totals.tenant_id
LEFT JOIN latest ON latest.tenant_id = totals.tenant_id
LEFT JOIN unpriced ON unpriced.tenant_id = totals.tenant_id;

ALTER VIEW public.projection_delegation_savings SET (security_invoker = true);
