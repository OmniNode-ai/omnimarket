-- OMN-17374: topology-derived omninode_runtime grant for tenant_registry_mirror.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_tenant_registry
--
-- ============================================================================
-- WHAT WAS ACTUALLY BROKEN (and why the ticket named the wrong role)
-- ============================================================================
--   OMN-17374 was filed against `tenant_projection_writer`, because the
--   reporter reproduced the failure by hand from inside the deployed container
--   using ONEX_TENANT_DB_URL. That is not the connection the runtime uses for
--   this relation, and fixing the role the ticket named would have granted a
--   principal that never touches this table while leaving the live failure
--   exactly where it was.
--
--   Both contracts classify this relation `omninode_internal`:
--     * node_projection_tenant_registry/contract.yaml  -> access: write
--     * node_projection_delegation/contract.yaml       -> access: read
--   so the runtime resolves BOTH its read and its write binding through
--   `DOMAIN_PROJECTION_BINDINGS[OMNINODE_INTERNAL]` = `omninode_runtime_service`
--   (dsn_env OMNINODE_INTERNAL_DB_URL, principal `omninode_runtime`) -- never
--   through `tenant_projection`. The classification is deliberate and
--   load-bearing; 0000's header explains at length why this relation must NOT
--   be tenant-classified, and that reasoning is unchanged here.
--
--   Proven live on the .201 dev lane 2026-09-01, inside `omninode-runtime`,
--   connecting with the runtime's own OMNINODE_INTERNAL_DB_URL:
--
--     current_user: omninode_runtime
--     select count(*) from tenant_registry_mirror
--       -> InsufficientPrivilegeError: permission denied for table
--          tenant_registry_mirror
--
--   and `information_schema.role_table_grants` for this relation returned
--   exactly three grantees -- `app_dashboard` (SELECT), `postgres`,
--   `role_omnidash` -- with `omninode_runtime` absent entirely.
--
-- ============================================================================
-- ONE MISSING GRANT, TWO REPORTED SYMPTOMS
-- ============================================================================
--   This single absence is the cause of BOTH halves of the reported failure,
--   which were filed as if they were independent:
--
--   1. READ. `node_projection_delegation`'s write-time identity resolution
--      (`omnimarket/projection/tenant_registry_resolution.py::
--      sync_registry_tenant_uuid`) issues `db.query('tenant_registry_mirror',
--      ...)`. That resolver swallows only a MISSING RELATION
--      (`_is_missing_relation`) and re-raises everything else, and an
--      InsufficientPrivilege is not a missing relation -- so the exception
--      propagates out of the handler before any resolution decision is made,
--      and the legacy-map fallback below it is never reached either. The
--      OMN-16804 change is therefore not monotonic on this lane, which is the
--      exact property its own module docstring claims.
--
--   2. WRITE. This node's own projection upserts through the same binding, so
--      its INSERT was refused by the same absent grant -- which is why
--      `tenant_registry_mirror` sat at 0 rows while its consumer group
--      reported Stable at LAG 0 against well-formed TENANT_CREATED events.
--      That silence is a separate defect (a projection that consumes an event
--      and writes no row must not ack it) and is owned by OMN-17379, not
--      repaired here. This file removes the CAUSE; OMN-17379 removes the
--      silence.
--
-- ============================================================================
-- WHY A MIGRATION, AND WHY THIS LINEAGE
-- ============================================================================
--   The topology ALREADY declares this grant. All three instances
--   (`omnibase_infra/src/omnibase_infra/topology/instances/{local,onex-dev,
--   onex-prod}.yaml`) carry `tenant_registry_mirror` in
--   `databases.application.principals.omninode_runtime.grants[object_type:
--   TABLE, schema: public]` with `[INSERT, SELECT, UPDATE]`, and that list is
--   itself GENERATED from node contract `db_io.db_tables` declarations by
--   `scripts/generate_application_database_table_grants.py --write`. What has
--   never existed is the half that issues it against a real database.
--
--   This is not a new defect class. `node_projection_session_replay/0002`
--   (OMN-16993) closed the identical gap for `session_replay_snapshots` and its
--   header states the residual in as many words: "The same gap exists for the
--   other 38 relations in that same topology grant list; each belongs in its
--   own node's migration." This file is that node's share for this relation --
--   it grants ONLY the relation this node owns, and claims nothing about the
--   rest. The corpus-wide residual is measured and ratcheted by
--   `scripts/validation/check_topology_grant_delivery.py`, which this change
--   also lands so the count can only shrink.
--
--   `omnibase_infra` flat migration 099 creates the role and grants it CONNECT
--   plus USAGE on `omninode_internal`. It cannot carry the `public`-schema
--   table half for a node-owned relation: the flat corpus is applied by a `psql
--   -f` loop gated on `directive_db == "$DB_NAME"`, and the node-owned loop is
--   the sanctioned path that connects directly to omnidash_analytics
--   (NODE_POSTGRES_DB). So the AUTHORIZATION rides here, in the lineage that
--   owns the relation, next to the 0000 that creates it.
--
-- ============================================================================
-- PRIVILEGES
-- ============================================================================
--   SELECT, INSERT, UPDATE and deliberately NO DELETE -- a projection writer
--   upserts, it does not reshape the table. That is the same invariant 096
--   states for role_omnidash, 099 states for this principal on
--   `omninode_internal.live_events`, and `0004_grant_tenant_projection_writer`
--   states for the tenant principal. PostgreSQL requires SELECT alongside
--   INSERT/UPDATE for the adapter's `INSERT ... ON CONFLICT DO UPDATE`, which
--   is why the write set is three privileges and not two.
--
--   This widens nothing beyond what the topology already declares. The mirror
--   holds no tenant's business data -- only the (slug, uuid, status)
--   correspondence the platform already publishes at signup and already puts
--   in the tenant's own JWT. It carries no RLS by design (0000's header
--   explains why putting RLS here would be fatal), so no policy interacts with
--   this grant and none is touched.
--
-- Physical location is bare `public`: 0000 issues an unqualified CREATE TABLE,
-- like every other relation still awaiting the OMN-15359 schema cutover, and
-- the topology's INTERNAL_TABLES_PHYSICALLY_IN_PUBLIC_UNTIL_OMN15359 bridge is
-- what maps the `omninode_internal` contract domain onto it. The grant must
-- name the PHYSICAL schema, so it says `public`.
--
-- Idempotency: GRANT is idempotent; re-running is a no-op. Nothing here touches
-- RLS, ownership, or any role attribute.

-- ---------------------------------------------------------------------------
-- 1. Schema USAGE, mirroring topology
--    `principals.omninode_runtime.grants[object_type: SCHEMA, schema: public]`.
--    Idempotent and re-asserted here for the same reason 099 re-asserts the
--    omninode_internal one: a migration must not assume a sibling file ran.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 2. Table grant (topology-derived, OMN-17374)
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON public.tenant_registry_mirror TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Assertions: fail the migration if either half did not take. Division by
--    zero when the grant is absent -- the same fail-loud shape 099,
--    node_log_persistence_effect/0000 and node_projection_session_replay/0002
--    already use.
--
--    BOTH directions are asserted, not just INSERT, because this relation is
--    the only one in the corpus whose read side is on the critical path of
--    another node: a lane where the INSERT landed and the SELECT did not would
--    fill the mirror and still fail every delegation write, which is the
--    harder failure to diagnose of the two.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_runtime_registry_mirror_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name = 'tenant_registry_mirror'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS omninode_runtime_registry_mirror_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name = 'tenant_registry_mirror'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';
