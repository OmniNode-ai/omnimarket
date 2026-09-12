-- OMN-17426: the two savings aggregate views carry their tenant, and the
-- overview's per-run payload carries the run's own identity.
--
-- WHAT THIS CLOSES
-- ----------------
-- `onex.snapshot.projection.delegation.savings.v1` and
-- `onex.snapshot.projection.cost.savings-overview.v1` are the two exposures the
-- customer arrival page reads, and both still answer `not_yet_bus_backed`
-- naming OMN-15800 -- a ticket that closed on 2026-08-24 having descoped these
-- families to "a follow-up ticket". This is that follow-up's database half.
--
-- Migration 088 landed the security half of the 2026-09-11 ruling and recorded,
-- in as many words, why the other half could not land with it: declaring
-- `tenant_column` on an exposure that is not `bus_backed` hard-fails contract
-- load, and re-grouping an aggregate nothing serves would invalidate its
-- `limit: 1` with no consumer to prove the new shape against. "Both halves land
-- together when the exposures convert." They convert here.
--
-- THE TENANT HALF
-- ---------------
-- Neither view had a `tenant_id` column, so neither could be published per
-- tenant. The precedent is node_projection_delegation/0039, which made the same
-- transformation on the four delegation aggregates for the same reason, and the
-- same three transformation rules apply here:
--
--   * every CTE that aggregates a base table gains `tenant_id` in its SELECT
--     and its GROUP BY;
--   * every nested aggregate gains it too, and the enclosing `jsonb_agg` groups
--     by it, so a JSON payload holds one tenant's rows and not a blend;
--   * every CROSS JOIN between those CTEs becomes a LEFT JOIN **on the tenant**,
--     anchored on the totals-shaped CTE -- a cross join would multiply tenants
--     together, which is the one transformation error that silently produces
--     plausible numbers.
--
-- The `LIMIT 500` / `LIMIT 20` truncations become windows PARTITIONed by tenant
-- for the reason 0039 records: a bare LIMIT after grouping returns rows shared
-- across all tenants and hands most tenants a truncated list.
--
-- `savings_estimates.tenant_id` is TEXT (080) and `delegation_events.tenant_id`
-- is uuid (node_projection_delegation/0034). The union needs one type, and the
-- output column is TEXT: it is compared as text by the writer's re-read
-- predicate and by `SnapshotCache.get_rows`, both of which hold the tenant as a
-- string. `::text` on the uuid side is exact -- uuid's text form is canonical
-- and lower-cased -- and it is the same direction onex-api's own
-- `delegation_savings.py` binds, which passes the SAME tenant twice under two
-- parameter types because these two columns disagree.
--
-- THE RUN-IDENTITY HALF (the overview only)
-- -----------------------------------------
-- `projection_cost_savings_overview.recent_runs` is the payload the arrival
-- page's Recent runs table renders. Every object in it was built from
-- `savings_estimates` alone and carried `session_id` + `savings_usd` and
-- nothing else that identifies the run: no correlation id, no cost, no gate,
-- no measured tokens, no latency. A reader keyed on the run's correlation id
-- therefore could not find the run it had just performed, and a reader
-- cross-checking the saved figure against `GET /v1/tenants/me/delegations` had
-- no field to compare.
--
-- Worse, the run could be absent entirely. `SavingsProjectionRunner
-- ._project_delegation_terminal` returns truthfully-empty (no row written, no
-- DLQ) when no counterfactual can be derived or the saving is <= 0, so a real
-- delegation can leave `delegation_events` populated and `savings_estimates`
-- empty. onex-api already handles that: it reads `delegation_events` and
-- LEFT JOIN LATERALs `savings_estimates` on `se.session_id = d.correlation_id`,
-- taking `COALESCE(se.savings_usd, d.cost_savings_usd)` as the run's saving.
--
-- This view now composes the SAME two sources under the SAME precedence --
-- a savings row wins where one exists for that correlation and tenant, and a
-- delegation row stands in where none does -- so the number the page renders
-- and the number the API returns for the same run are the same number, derived
-- the same way, rather than two independently plausible ones. That union is
-- not invented here either: `projection_delegation_savings` (083/087) has
-- composed these two sources this way since OMN-15533.
--
-- The KPI totals move onto the same combined set, because the alternative is a
-- single row whose `recent_runs` lists runs its own `total_savings_usd` does
-- not count. `tokens_total`, `tokens_to_compliance` and `measured_run_count`
-- stop being the hardcoded zeros 077 shipped -- `delegation_events` measures
-- them -- and `local_token_pct` does NOT, because nothing measures a local/cloud
-- token split; it stays zero and now says so in `warnings` rather than being
-- silently indistinguishable from a measured zero.
--
-- ADDITIVE JSON, NOT RENAMED JSON. Every key `recent_runs` objects carried
-- before is still there and still means what it meant. `correlation_id`,
-- `cost_usd`, `cost_savings_usd`, `quality_gate_passed`, `tokens_to_compliance`
-- and `cost_tier_name` are added beside them. A consumer reading `session_id`
-- and `savings_usd` is unaffected; that is deliberate, because omnidash reads
-- this payload too and is not re-pointed in this change.
--
-- WHY `CREATE OR REPLACE` AND NOT A DROP
-- --------------------------------------
-- Postgres permits `CREATE OR REPLACE VIEW` to APPEND output columns; it
-- refuses a rename, a retype or a reorder. `tenant_id` is appended last on both
-- views and every pre-existing column keeps its name, type and position, so no
-- DROP is needed -- which means no grant is discarded and no `security_invoker`
-- setting is lost. 0039 had to DROP because it inserted `tenant_id` mid-list;
-- paying that price here would be a choice, not a constraint.
--
-- The one option that IS set here is `security_invoker` on
-- `projection_cost_savings_overview`, which 088 left behind: it altered the two
-- views that read `delegation_events` and did not alter this one, which read
-- only `savings_estimates` at the time. It reads `delegation_events` as of this
-- migration, and it is about to be re-read by a writer under a tenant scope, so
-- it inherits 088's whole argument. That also closes the last open case of the
-- VIEW CAVEAT migration 081 recorded against all three savings views.
--
-- GRANTS: additive, and issued rather than assumed. The re-read that publishes
-- these snapshots runs as the savings writer, and 081 deliberately granted
-- app_dashboard nothing on the views while they were owner-scoped. With
-- `security_invoker` set on all three, a grant no longer widens what a reader
-- can see -- the base-table policy is evaluated for the caller either way -- so
-- the two projection readers are granted SELECT explicitly, the same pair and
-- the same reasoning as 0039's grant block.

-- ---------------------------------------------------------------------------
-- 1. projection_delegation_savings -- per tenant.
-- ---------------------------------------------------------------------------
-- 087's body, with tenant_id threaded through every CTE and appended to the
-- output. The three provenance expressions and every other column are 087's,
-- reproduced so the diff is exactly the tenant transformation.

CREATE OR REPLACE VIEW public.projection_delegation_savings AS
WITH savings_sessions AS (
    SELECT
        tenant_id::text AS tenant_id,
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
),
event_sessions AS (
    SELECT
        tenant_id::text AS tenant_id,
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
),
combined_sessions AS (
    SELECT * FROM savings_sessions
    UNION ALL
    SELECT event_sessions.*
    FROM event_sessions
    WHERE NOT EXISTS (
        -- OMN-17426: the dedup key gains the tenant. Without it, one tenant's
        -- savings row suppresses another tenant's delegation row that happens
        -- to share a session id -- a cross-tenant effect from a key that
        -- looked tenant-agnostic.
        SELECT 1
        FROM savings_sessions
        WHERE savings_sessions.session_id = event_sessions.session_id
          AND savings_sessions.tenant_id = event_sessions.tenant_id
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
    SELECT
        tenant_id, session_id, task_type, model_name, local_cost_usd,
        cloud_cost_usd, counterfactual_baseline_usd, savings_usd,
        baseline_model, pricing_manifest_version, savings_method, usage_source,
        prompt_tokens, completion_tokens, tokens_to_compliance, latency_ms,
        created_at, prompt_text, response_text
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
)
SELECT
    totals.cumulative_savings_usd,
    totals.cumulative_local_cost_usd,
    totals.cumulative_cloud_cost_usd,
    COALESCE(latest.baseline_model, 'claude-opus-4.1') AS baseline_model,
    COALESCE(latest.pricing_manifest_version, 'runtime-delegation-events')
        AS pricing_manifest_version,
    totals.session_count,
    COALESCE(sessions.rows, '[]'::jsonb) AS sessions,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    TRUE AS provisioned,
    totals.latest_projection_updated_at,
    totals.cumulative_counterfactual_baseline_usd,
    totals.tenant_id
FROM totals
LEFT JOIN sessions ON sessions.tenant_id = totals.tenant_id
LEFT JOIN latest ON latest.tenant_id = totals.tenant_id;


-- ---------------------------------------------------------------------------
-- 2. projection_cost_savings_overview -- per tenant, and per-run identity.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW public.projection_cost_savings_overview AS
WITH savings_runs AS (
    SELECT
        tenant_id::text AS tenant_id,
        -- onex-api joins `savings_estimates.session_id` to
        -- `delegation_events.correlation_id`; this view composes the same two
        -- sources on the same key, so the id a reader matches on is the same
        -- id the API matched on.
        session_id AS correlation_id,
        session_id,
        COALESCE(NULLIF(task_type, ''), 'savings-estimated') AS task_type,
        COALESCE(NULLIF(model_local, ''), 'local') AS model_id,
        COALESCE(NULLIF(model_local, ''), 'Local model') AS display_name,
        local_cost_usd::float AS cost_usd,
        cloud_cost_usd::float AS baseline_cost_usd,
        savings_usd::float AS savings_usd,
        COALESCE(prompt_tokens, 0)::int AS prompt_tokens,
        COALESCE(completion_tokens, 0)::int AS completion_tokens,
        NULL::int AS tokens_to_compliance,
        NULL::int AS latency_ms,
        NULL::boolean AS quality_gate_passed,
        NULL::text AS cost_tier_name,
        COALESCE(usage_source, 'unknown') AS token_provenance,
        COALESCE(updated_at, created_at, event_timestamp)::timestamptz
            AS projected_at
    FROM public.savings_estimates
),
event_runs AS (
    SELECT
        tenant_id::text AS tenant_id,
        COALESCE(NULLIF(correlation_id, ''), NULLIF(session_id, ''), id::text)
            AS correlation_id,
        COALESCE(NULLIF(session_id, ''), NULLIF(correlation_id, ''), id::text)
            AS session_id,
        COALESCE(NULLIF(task_type, ''), 'delegation') AS task_type,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'local')
            AS model_id,
        COALESCE(NULLIF(model_name, ''), NULLIF(delegated_to, ''), 'Local model')
            AS display_name,
        COALESCE(cost_usd, 0)::float AS cost_usd,
        (COALESCE(cost_usd, 0) + COALESCE(cost_savings_usd, 0))::float
            AS baseline_cost_usd,
        COALESCE(cost_savings_usd, 0)::float AS savings_usd,
        COALESCE(tokens_input, 0)::int AS prompt_tokens,
        COALESCE(tokens_output, 0)::int AS completion_tokens,
        NULLIF(tokens_to_compliance, 0)::int AS tokens_to_compliance,
        COALESCE(delegation_latency_ms, latency_ms)::int AS latency_ms,
        quality_gate_passed,
        cost_tier_name::text AS cost_tier_name,
        CASE
            WHEN cost_measurement_source IN (
                'metered', 'free_local', 'budgeted_in_budget',
                'budgeted_overage', 'budgeted_split') THEN 'measured'
            WHEN cost_measurement_source = 'manifest_compute' THEN 'estimated'
            ELSE 'unknown'
        END AS token_provenance,
        COALESCE(created_at, timestamp)::timestamptz AS projected_at
    FROM public.delegation_events
),
combined_runs AS (
    -- Savings rows win, delegation rows stand in. Identical precedence to
    -- onex-api's COALESCE(se.savings_usd, d.cost_savings_usd), so the figure
    -- this page renders for a run and the figure that API returns for the
    -- same run are one number, not two agreeing ones.
    SELECT * FROM savings_runs
    UNION ALL
    SELECT event_runs.*
    FROM event_runs
    WHERE NOT EXISTS (
        SELECT 1
        FROM savings_runs
        WHERE savings_runs.correlation_id = event_runs.correlation_id
          AND savings_runs.tenant_id = event_runs.tenant_id
    )
),
totals AS (
    SELECT
        tenant_id,
        COALESCE(SUM(cost_usd), 0)::float AS total_cost_usd,
        COALESCE(SUM(baseline_cost_usd), 0)::float AS total_baseline_cost_usd,
        COALESCE(SUM(savings_usd), 0)::float AS total_savings_usd,
        COALESCE(SUM(prompt_tokens + completion_tokens), 0)::int AS tokens_total,
        COALESCE(SUM(COALESCE(tokens_to_compliance, 0)), 0)::int
            AS tokens_to_compliance,
        COUNT(*) FILTER (
            WHERE prompt_tokens + completion_tokens > 0
        )::int AS measured_run_count,
        COUNT(*) FILTER (
            WHERE prompt_tokens + completion_tokens = 0
        )::int AS zero_token_run_count,
        COUNT(*)::int AS run_count,
        MAX(projected_at) AS latest_projection_updated_at
    FROM combined_runs
    GROUP BY tenant_id
),
model_rows AS (
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'model_id', model_id,
                    'display_name', display_name,
                    'execution_mode', 'delegated',
                    'task_count', task_count,
                    'tokens_total', tokens_total,
                    'cost_usd', cost_usd,
                    'baseline_cost_usd', baseline_cost_usd,
                    'savings_usd', savings_usd,
                    'savings_pct', CASE WHEN baseline_cost_usd > 0
                        THEN savings_usd / baseline_cost_usd ELSE 0 END,
                    'runtime_address', NULL,
                    'evidence_ref', NULL
                )
                ORDER BY savings_usd DESC, display_name
            ),
            '[]'::jsonb
        ) AS rows
    FROM (
        SELECT
            tenant_id,
            model_id,
            display_name,
            COUNT(*)::int AS task_count,
            COALESCE(SUM(prompt_tokens + completion_tokens), 0)::int
                AS tokens_total,
            COALESCE(SUM(cost_usd), 0)::float AS cost_usd,
            COALESCE(SUM(baseline_cost_usd), 0)::float AS baseline_cost_usd,
            COALESCE(SUM(savings_usd), 0)::float AS savings_usd
        FROM combined_runs
        GROUP BY tenant_id, model_id, display_name
    ) grouped_models
    GROUP BY tenant_id
),
ranked_runs AS (
    SELECT
        combined_runs.*,
        ROW_NUMBER() OVER (
            PARTITION BY tenant_id ORDER BY projected_at DESC
        ) AS tenant_rank
    FROM combined_runs
),
recent_runs AS (
    SELECT
        tenant_id,
        COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    -- Pre-existing keys, unchanged in name and meaning.
                    'session_id', session_id,
                    'task_type', task_type,
                    'model_name', display_name,
                    'prompt_tokens', prompt_tokens,
                    'completion_tokens', completion_tokens,
                    'total_tokens', prompt_tokens + completion_tokens,
                    'savings_usd', savings_usd,
                    'latency_ms', latency_ms,
                    'created_at', projected_at,
                    'token_provenance', token_provenance,
                    -- OMN-17426, added beside them: what identifies the run,
                    -- what it cost, and whether it passed its gate.
                    'correlation_id', correlation_id,
                    'cost_usd', cost_usd,
                    'cost_savings_usd', savings_usd,
                    'quality_gate_passed', quality_gate_passed,
                    'tokens_to_compliance', tokens_to_compliance,
                    'cost_tier_name', cost_tier_name
                )
                ORDER BY projected_at DESC
            ),
            '[]'::jsonb
        ) AS rows
    FROM ranked_runs
    WHERE tenant_rank <= 20
    GROUP BY tenant_id
),
warnings AS (
    SELECT
        tenant_id,
        (
            CASE WHEN zero_token_run_count > 0 THEN jsonb_build_array(
                zero_token_run_count
                || ' run(s) carry no measured token counts; they are counted in'
                || ' cost and savings but excluded from the token KPIs'
            ) ELSE '[]'::jsonb END
            ||
            CASE WHEN run_count > 0 THEN jsonb_build_array(
                'No source measures a local/cloud token split, so'
                || ' local_token_pct is reported as zero rather than measured'
            ) ELSE '[]'::jsonb END
        ) AS rows
    FROM totals
)
SELECT
    'all'::text AS "window",
    totals.total_cost_usd,
    totals.total_baseline_cost_usd,
    totals.total_savings_usd,
    CASE WHEN totals.total_baseline_cost_usd > 0
        THEN totals.total_savings_usd / totals.total_baseline_cost_usd
        ELSE 0
    END AS savings_rate,
    totals.tokens_total,
    totals.tokens_to_compliance,
    0::float AS local_token_pct,
    COALESCE(totals.latest_projection_updated_at, NOW()) AS captured_at,
    COALESCE(model_rows.rows, '[]'::jsonb) AS rows,
    COALESCE(recent_runs.rows, '[]'::jsonb) AS recent_runs,
    totals.measured_run_count,
    totals.zero_token_run_count,
    COALESCE(warnings.rows, '[]'::jsonb) AS warnings,
    (totals.run_count > 0) AS provisioned,
    totals.latest_projection_updated_at,
    totals.tenant_id
FROM totals
LEFT JOIN model_rows ON model_rows.tenant_id = totals.tenant_id
LEFT JOIN recent_runs ON recent_runs.tenant_id = totals.tenant_id
LEFT JOIN warnings ON warnings.tenant_id = totals.tenant_id;


-- ---------------------------------------------------------------------------
-- 3. Invoker rights on the overview -- 088's remaining case.
-- ---------------------------------------------------------------------------
-- Idempotent: `ALTER VIEW ... SET` is a no-op when the option already holds.
-- The two views 088 altered keep theirs; `CREATE OR REPLACE` above did not
-- disturb them, which is the second reason it is a replace and not a drop.

ALTER VIEW public.projection_cost_savings_overview SET (security_invoker = true);

-- ---------------------------------------------------------------------------
-- 4. Grants for the two projection readers.
-- ---------------------------------------------------------------------------
-- Named individually rather than issued over the schema, so a reviewer can
-- point at the statement that delivers each privilege.

GRANT SELECT ON public.projection_delegation_savings TO app_dashboard;
GRANT SELECT ON public.projection_cost_savings_overview TO app_dashboard;
GRANT SELECT ON public.projection_delegation_savings TO tenant_projection_writer;
GRANT SELECT ON public.projection_cost_savings_overview TO tenant_projection_writer;
