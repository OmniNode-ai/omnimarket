-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- OMN-26444: the shared savings view counts a run unpriced by the shared metering
-- refresh's rule, so delegation.savings.v1's unpriced_call_count equals the refresh's
-- runs_unknown_spend for the same tenant (decisions D1, D2 option A and D3,
-- 2026-11-22).
--
-- 101 counted every run whose stored cost_usd was NULL. The refresh
-- (handler_metering_summary_store_refresh.py) counts unknown spend only for runs
-- whose data_source is not fixture or experiment, that were not returned to the
-- caller, that recorded tokens, and whose cost has no named price (priced_spend).
-- A run whose session also has a priced savings_estimates row is still unpriced by that
-- rule, as it is for the refresh, so the count no longer excludes it (101 did).
-- This revision changes only that flag. Every served column keeps its name, type
-- and position; the flag stays inside the one combined_sessions pass (OMN-22675).
--
-- The named-price rule needs the manifest in the database: savings_priced_models
-- holds each model the pricing manifest names and whether it is free. The
-- projection writer refills it from the manifest (refresh_shared_priced_models);
-- the view carries no literal price. data_source is NOT NULL DEFAULT 'real' here
-- (node_projection_delegation 0049, 0064), so no NULL case exists on this path.

CREATE TABLE IF NOT EXISTS public.savings_priced_models (
    model_key TEXT PRIMARY KEY,
    free BOOLEAN NOT NULL
);

-- ---- BEGIN OMN-15376 shape reconciliation: savings_priced_models ----
-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so CREATE
-- TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE public.savings_priced_models ADD COLUMN IF NOT EXISTS model_key TEXT;
ALTER TABLE public.savings_priced_models ADD COLUMN IF NOT EXISTS free BOOLEAN;

-- NOT NULL RECONCILIATION: SET NOT NULL refuses (fails the migration) when a
-- pre-existing row holds NULL; it never invents a value.
ALTER TABLE public.savings_priced_models ALTER COLUMN model_key SET NOT NULL;
ALTER TABLE public.savings_priced_models ALTER COLUMN free SET NOT NULL;

-- PRIMARY KEY RECONCILIATION.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.savings_priced_models'::regclass AND contype = 'p'
    ) THEN
        ALTER TABLE public.savings_priced_models
            ADD CONSTRAINT savings_priced_models_pkey
            PRIMARY KEY (model_key);
    END IF;
END$$;
-- ---- END OMN-15376 shape reconciliation: savings_priced_models ----

GRANT SELECT ON public.savings_priced_models TO app_dashboard;
GRANT SELECT, INSERT, UPDATE ON public.savings_priced_models TO tenant_projection_writer;
GRANT DELETE ON public.savings_priced_models TO tenant_projection_writer;

CREATE OR REPLACE VIEW public.projection_delegation_savings AS
WITH raw_savings_sessions AS (
    SELECT
        id,
        COALESCE(updated_at, created_at, event_timestamp)::timestamptz
            AS projected_at,
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
                ORDER BY projected_at DESC, id DESC
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
        response_text,
        (
            -- priced_spend's rule: a stored cost is spend only when the manifest
            -- names the model that ran (exact key, else lower case), and a positive
            -- cost on a model it lists as free is not a price.
            (
                cost_usd IS NULL
                OR COALESCE(pm_exact.free, pm_lower.free) IS NULL
                OR (cost_usd > 0 AND COALESCE(pm_exact.free, pm_lower.free))
            )
            -- The runs the shared metering refresh folds into runs_unknown_spend:
            -- real data, not returned to the caller, tokens in either direction.
            AND data_source NOT IN ('fixture', 'experiment')
            AND terminal_failure_cause IS DISTINCT FROM 'returned_to_caller'
            AND (COALESCE(tokens_input, 0) <> 0 OR COALESCE(tokens_output, 0) <> 0)
        ) AS unpriced_run
    FROM public.delegation_events
    LEFT JOIN public.savings_priced_models AS pm_exact
      ON pm_exact.model_key
         = COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), '')
    LEFT JOIN public.savings_priced_models AS pm_lower
      ON pm_lower.model_key
         = lower(COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), ''))
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
        event_sessions.prompt_tokens,
        event_sessions.completion_tokens,
        event_sessions.tokens_to_compliance,
        event_sessions.latency_ms,
        COALESCE(
            GREATEST(event_sessions.created_at, savings_sessions.created_at),
            event_sessions.created_at,
            savings_sessions.created_at
        ) AS created_at,
        event_sessions.prompt_text,
        event_sessions.response_text,
        event_sessions.unpriced_run
    FROM event_sessions
    LEFT JOIN savings_sessions
      ON savings_sessions.session_id = event_sessions.session_id
     AND savings_sessions.tenant_id = event_sessions.tenant_id
    UNION ALL
    SELECT
        savings_sessions.*,
        FALSE AS unpriced_run
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
        COALESCE(SUM(savings_usd) FILTER (WHERE usage_source = 'measured'), 0)::float AS cumulative_savings_usd,
        COALESCE(SUM(local_cost_usd) FILTER (WHERE usage_source = 'measured'), 0)::float AS cumulative_local_cost_usd,
        COALESCE(SUM(cloud_cost_usd) FILTER (WHERE usage_source = 'measured'), 0)::float AS cumulative_cloud_cost_usd,
        COALESCE(SUM(counterfactual_baseline_usd) FILTER (WHERE usage_source = 'measured'), 0)::float
            AS cumulative_counterfactual_baseline_usd,
        COUNT(*)::int AS session_count,
        COUNT(*) FILTER (WHERE usage_source = 'measured')::int
            AS measured_run_count,
        COUNT(*) FILTER (WHERE usage_source = 'estimated')::int
            AS estimated_run_count,
        COUNT(*) FILTER (WHERE usage_source = 'unknown')::int
            AS unknown_run_count,
        MAX(created_at) AS latest_projection_updated_at,
        COUNT(*) FILTER (WHERE unpriced_run)::int AS unpriced_call_count
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
    totals.unpriced_call_count,
    totals.measured_run_count,
    totals.estimated_run_count,
    totals.unknown_run_count
FROM totals
LEFT JOIN sessions ON sessions.tenant_id = totals.tenant_id
LEFT JOIN latest ON latest.tenant_id = totals.tenant_id;

ALTER VIEW public.projection_delegation_savings SET (security_invoker = true);
