-- OMN-15631 v1(a): per-tenant delegation routing overlay (dev/beta, NO RLS).
--
-- Design-feasibility assessment (ticket comment 41f99997) split AC2
-- (DB-enforced cross-tenant denial) into a follow-on ticket blocked on
-- OMN-14894/OMN-15356 (the `tenant`-schema RLS foundation), because that
-- foundation does not exist yet for this domain. This table intentionally
-- ships WITHOUT RLS: it is the v1(a) staging shape, additive so that once the
-- RLS foundation lands, promoting these rows into the RLS-backed `tenant.*`
-- home is a data MOVE (INSERT INTO tenant.delegation_routing_overlay SELECT
-- ... FROM delegation_routing_tenant_overlay), never a redesign. Do not add
-- RLS policies to this table directly -- that is explicitly out of scope for
-- v1(a) and belongs on the follow-on ticket.
--
-- Row shape and precedence semantics (AC6 seam, PR-body deliverable):
--   - One row per (tenant_id, task_type). A tenant may override the fully
--     resolved BACKEND BINDING (endpoint_url, model_name, secret_ref,
--     timeout_ms, max_tokens) for a given task_type -- WHOLESALE, not a
--     per-field deep-merge against the platform default. Mixing a tenant's
--     endpoint_url with the platform's model_name (or vice versa) would
--     silently address the wrong provider with the wrong model id, so a
--     matching overlay row fully REPLACES the platform-resolved backend for
--     that (tenant_id, task_type) pair rather than merging field-by-field.
--   - Routing STRUCTURE -- which tiers exist, tier_order, escalation policy,
--     pricing ceilings, cloud_routing_policy -- remains PLATFORM-FIXED in
--     v1(a) and is NOT represented in this table. A tenant overlay row is
--     consulted BEFORE tier/contract selection runs for that task_type: when
--     present it short-circuits tier iteration entirely (the tenant has
--     opted into a specific backend for that task class, not into the
--     platform's escalation ladder). When absent, resolution falls through
--     unchanged to the existing platform-default tier/contract logic --
--     this is what keeps AC3 (no-overlay resolves platform default) and AC4
--     (tenant-zero unchanged) true by construction, not by extra branching.
--   - secret_ref is nullable (an unauthenticated backend is a valid
--     configuration) but when present is resolved ONLY at the effect
--     boundary via ProtocolSecretStore, fail-fast on a miss -- this table
--     never stores a secret VALUE, only the ref name.
--   - No api_key_env column: the house env-var fallback
--     (BifrostBackendRef.api_key_env) is an OmniNode-house-deployment
--     convention. Tenant-overlay-sourced backends must never silently fall
--     back to a house key when their own ref is unresolved -- omitting the
--     column is what makes that impossible to wire in by accident.
--
-- tenant_id is TEXT (not UUID), matching the landed convention on every other
-- tenant_id column in this repo today (delegation_events 0022, llm_cost_
-- aggregates 0002, etc.) -- see omnimarket.projection.tenant_isolation
-- (HOUSE_TENANT_SLUG = 'omninode'). No FK to a `tenant` schema table: that
-- schema/RLS foundation is exactly what OMN-14894/OMN-15356 build, and this
-- table must not preempt or collide with OMN-15354's schema classification.
--
-- WHY BARE delegation_routing_tenant_overlay, LOGICALLY tenant-DOMAIN
-- (RULING 2026-08-20, resolves the schema-qualification blocker for good)
--
--   Two prior attempts on this file both failed, for two DIFFERENT reasons,
--   and the fix is neither of them -- it is the same bridge every other
--   tenant-domain table in this corpus already rides:
--
--   Attempt 1 (schema-qualify to `tenant.delegation_routing_tenant_overlay`,
--   with a divide-by-zero precondition asserting the `tenant` schema exists):
--   FAILS `tests/scripts/test_node_migration_fence_parity.py::
--   test_virgin_database_applies_the_full_real_vendored_tree` (local repro,
--   2026-08-20: `ERROR: division by zero` at the precondition line). The
--   `tenant` Postgres schema is created NOWHERE in this migration corpus --
--   confirmed by grep across docker/migrations/forward/**/*.sql in the
--   companion omnibase_infra repo, and by omnibase_infra#2803's (OMN-16239)
--   live 2026-08-19 readback of the stability-test lane's omnidash_analytics
--   database: its only non-system schemas are `information_schema`,
--   `omninode_internal`, `public` -- there is no `tenant` schema anywhere,
--   on any lane, today. Unlike `omninode_internal` (physically created by
--   omnibase_infra's docker/migrations/forward/098_create_omninode_internal_
--   schema.sql, a real `CREATE SCHEMA IF NOT EXISTS omninode_internal`), no
--   equivalent root migration for `tenant` exists, so an ASSERT-only
--   precondition against `tenant` can never pass on any lane.
--
--   Attempt 2 (bare table, `schema: unresolved` in contract.yaml): fails the
--   static gate outright -- `scripts/ci/check_application_database_sql.py`
--   (OMN-15361/OMN-15423) unconditionally rejects any NEW bare/public
--   application relation with no schema-domain awareness at all. Declaring
--   `schema: unresolved` also does not reflect reality: the DOMAIN is known
--   (tenant-attributable workload data, ADR-0027) -- only the PHYSICAL
--   location was ever in question.
--
--   THE ACTUAL RESOLUTION: `delegation_events` -- this table's own sibling,
--   same tenant domain, same house-tenant ruling 2026-08-02 -- is ALSO bare/
--   unqualified in `public` today (src/omnimarket/nodes/node_projection_
--   delegation/migrations/0007_delegation_events.sql), and is enumerated in
--   omnibase_infra's `TENANT_TABLES_PHYSICALLY_IN_PUBLIC_UNTIL_OMN15359`
--   (src/omnibase_infra/topology/physical_schema_mapping.py) -- the SAME
--   allowlist the runtime grants system already trusts via
--   `physical_grant_schema_for_table()` to resolve a logically-tenant table
--   to its real physical schema (`public`) until OMN-15359 performs the
--   governed per-family copy into a real `tenant.*` home. This table takes
--   the identical bridge: bare/public physically, `schema: tenant` logically
--   in contract.yaml (unchanged by this fix -- see that file), and
--   enumerated in the same allowlist (omnibase_infra#2820 companion PR).
--
--   The static gate does not consult that allowlist YET -- that is exactly
--   what omnibase_infra#2802 (OMN-16237, "domain enforcement gate consults
--   physical-schema allowlist") teaches it to do, importing the same two
--   frozensets `lint_application_database_sql` currently has no knowledge
--   of. omnibase_infra#2803 (OMN-16239) is the companion fix that makes the
--   RUNTIME SQL-emission path (handler_wiring.py's `_execute_upsert`/
--   `_execute_query`) resolve against the physical schema instead of the
--   raw declared one -- without it, `schema: tenant` in the contract would
--   make the runtime emit `INSERT INTO "tenant"."delegation_routing_tenant_
--   overlay"` against a schema that does not exist. Both PRs are open,
--   otherwise green, and blocked only by an unrelated systemic CI Summary
--   outage as of this writing -- this PR's static-gate check goes green
--   without further edits once #2802 merges and this branch rebases; #2803
--   is a prerequisite for this table to be safely read/written at runtime.
--   OMN-16314 still owns the later RLS foundation/promotion work untouched
--   by any of this.

CREATE TABLE IF NOT EXISTS delegation_routing_tenant_overlay (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    backend_id TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    model_name TEXT NOT NULL,
    secret_ref TEXT,
    timeout_ms INTEGER
        CONSTRAINT delegation_routing_tenant_overlay_timeout_ms_positive
        CHECK (timeout_ms IS NULL OR timeout_ms > 0),
    max_tokens INTEGER
        CONSTRAINT delegation_routing_tenant_overlay_max_tokens_positive
        CHECK (max_tokens IS NULL OR max_tokens > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT delegation_routing_tenant_overlay_tenant_task_uq
        UNIQUE (tenant_id, task_type)
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_routing_tenant_overlay ----
-- CREATE TABLE IF NOT EXISTS silently no-ops against a drifted pre-existing
-- table; the guarded adds below converge such a table onto the shape declared
-- above (no-ops on the fresh-create path, since every column already exists
-- there). No DROP, no recreate, no TRUNCATE. Matches capability_scores'
-- (node_canary_score_reducer/0001) and projection_watermarks'
-- (node_projection_registration/0005) own precedent.
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS id BIGSERIAL;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS task_type TEXT;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS backend_id TEXT;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS endpoint_url TEXT;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS model_name TEXT;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS secret_ref TEXT;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS timeout_ms INTEGER;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS max_tokens INTEGER;
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE delegation_routing_tenant_overlay ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

WITH next_overlay_ids AS (
    SELECT
        ctid,
        COALESCE((SELECT max(id) FROM delegation_routing_tenant_overlay), 0)
            + row_number() OVER (ORDER BY ctid) AS reconciled_id
    FROM delegation_routing_tenant_overlay
    WHERE id IS NULL
)
UPDATE delegation_routing_tenant_overlay AS overlay
SET id = next_overlay_ids.reconciled_id
FROM next_overlay_ids
WHERE overlay.ctid = next_overlay_ids.ctid;

WITH duplicate_ids AS (
    SELECT
        ctid,
        row_number() OVER (PARTITION BY id ORDER BY ctid) AS duplicate_rank,
        row_number() OVER (ORDER BY id, ctid) AS global_duplicate_rank
    FROM delegation_routing_tenant_overlay
)
UPDATE delegation_routing_tenant_overlay AS overlay
SET id = COALESCE((SELECT max(id) FROM delegation_routing_tenant_overlay), 0)
    + duplicate_ids.global_duplicate_rank
FROM duplicate_ids
WHERE overlay.ctid = duplicate_ids.ctid
  AND duplicate_ids.duplicate_rank > 1;

UPDATE delegation_routing_tenant_overlay
SET
    tenant_id = COALESCE(NULLIF(tenant_id, ''), '__reconciled_missing_tenant_' || id::TEXT),
    task_type = COALESCE(NULLIF(task_type, ''), '__reconciled_missing_task_' || id::TEXT),
    backend_id = COALESCE(NULLIF(backend_id, ''), '__reconciled_missing_backend_' || id::TEXT),
    endpoint_url = COALESCE(NULLIF(endpoint_url, ''), 'disabled://reconciled-missing-endpoint/' || id::TEXT),
    model_name = COALESCE(NULLIF(model_name, ''), '__reconciled_missing_model_' || id::TEXT),
    created_at = COALESCE(created_at, NOW()),
    updated_at = COALESCE(updated_at, NOW());

UPDATE delegation_routing_tenant_overlay
SET timeout_ms = NULL
WHERE timeout_ms IS NOT NULL AND timeout_ms <= 0;

UPDATE delegation_routing_tenant_overlay
SET max_tokens = NULL
WHERE max_tokens IS NOT NULL AND max_tokens <= 0;

WITH duplicate_overlay_keys AS (
    SELECT
        ctid,
        id,
        row_number() OVER (PARTITION BY tenant_id, task_type ORDER BY id, ctid) AS duplicate_rank
    FROM delegation_routing_tenant_overlay
)
UPDATE delegation_routing_tenant_overlay AS overlay
SET task_type = overlay.task_type || '__reconciled_duplicate_' || duplicate_overlay_keys.id::TEXT
FROM duplicate_overlay_keys
WHERE overlay.ctid = duplicate_overlay_keys.ctid
  AND duplicate_overlay_keys.duplicate_rank > 1;

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_timeout_ms_positive;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT delegation_routing_tenant_overlay_timeout_ms_positive
        CHECK (timeout_ms IS NULL OR timeout_ms > 0);

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_max_tokens_positive;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT delegation_routing_tenant_overlay_max_tokens_positive
        CHECK (max_tokens IS NULL OR max_tokens > 0);

ALTER TABLE delegation_routing_tenant_overlay
    ALTER COLUMN id SET NOT NULL,
    ALTER COLUMN tenant_id SET NOT NULL,
    ALTER COLUMN task_type SET NOT NULL,
    ALTER COLUMN backend_id SET NOT NULL,
    ALTER COLUMN endpoint_url SET NOT NULL,
    ALTER COLUMN model_name SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW(),
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_pkey;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT delegation_routing_tenant_overlay_pkey PRIMARY KEY (id);

ALTER TABLE delegation_routing_tenant_overlay
    DROP CONSTRAINT IF EXISTS delegation_routing_tenant_overlay_tenant_task_uq;
ALTER TABLE delegation_routing_tenant_overlay
    ADD CONSTRAINT delegation_routing_tenant_overlay_tenant_task_uq UNIQUE (tenant_id, task_type);
-- ---- END OMN-15376 shape reconciliation: delegation_routing_tenant_overlay ----

CREATE INDEX IF NOT EXISTS idx_delegation_routing_tenant_overlay_tenant_id
    ON delegation_routing_tenant_overlay (tenant_id);
