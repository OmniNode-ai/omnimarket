-- OMN-16993: topology-derived omninode_runtime grant for session_replay_snapshots.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_projection_session_replay
--
-- WHY THIS EXISTS
--   OMN-16843 re-pointed every `database_ref: application` projection at the
--   non-BYPASSRLS `omninode_runtime` principal via OMNINODE_INTERNAL_DB_URL.
--   Before that, this projection connected as `role_omnidash`, whose blanket
--   `GRANT ... ON ALL TABLES IN SCHEMA public` comes from the bootstrap script
--   `000_create_multiple_databases.sh`. `omninode_runtime` deliberately does
--   NOT go through that path -- the bootstrap's own LOGIN_ONLY_ROLE_MAP note
--   explains why: `grant_role_to_database()` issues `CREATE ON SCHEMA public`,
--   and a table's owner is exempt from row-level security UNCONDITIONALLY,
--   FORCE included. Its authorization is owned by the topology instead.
--
--   The topology does declare it. All three instances
--   (`omnibase_infra/src/omnibase_infra/topology/instances/{local,onex-dev,onex-prod}.yaml`)
--   carry `session_replay_snapshots` in
--   `databases.application.principals.omninode_runtime.grants[object_type: TABLE, schema: public]`
--   with `[INSERT, SELECT, UPDATE]`. What was missing is the half that issues
--   it against a real database: no migration in either repo ever granted this
--   principal anything in schema `public`. Verified live 2026-08-29 -- on the
--   .201 stability lane `information_schema.role_table_grants` returned grants
--   for `omninode_runtime` on nine `omninode_internal` relations and ZERO
--   relations in `public`.
--
--   Consequence, observed live: once the role could authenticate at all, this
--   projection's every write failed `InsufficientPrivilege: permission denied
--   for table session_replay_snapshots` and routed to the platform quarantine
--   sink, while the runtime kept reporting healthy and committing offsets.
--
-- SCOPE
--   This file grants ONLY this node's own relation. The same gap exists for the
--   other 38 relations in that same topology grant list; each belongs in its own
--   node's migration, exactly as `node_log_persistence_effect/0000` (OMN-15846),
--   `node_projection_registration/0005` (OMN-16146) and
--   `node_savings_estimation_compute/0001` (OMN-16293) already do for theirs.
--
-- PRIVILEGES
--   SELECT, INSERT, UPDATE and no DELETE -- a projection writer upserts, it does
--   not reshape the table. Same invariant 096 states for role_omnidash and 099
--   states for this principal on `omninode_internal.live_events`.
--
-- Idempotency: GRANT is idempotent; re-running is a no-op.

-- ---------------------------------------------------------------------------
-- 1. Fail loud if the principal is absent rather than recording this migration
--    against a role that does not exist. `099_create_omninode_internal_live_events.sql`
--    guard-creates it (NOLOGIN; the LOGIN + password attach is deployment-owned,
--    OMN-16993) and sorts ahead of the node tree in the runner, so an absent
--    role here means the flat set never ran -- a real defect, not a lane variant.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omninode_runtime') THEN
    RAISE EXCEPTION
      'omninode_runtime role does not exist -- 099_create_omninode_internal_live_events.sql '
      'guard-creates it and must have run before the node migration tree. This '
      'migration refuses to record itself against a role that is not there.';
  END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. Schema USAGE, mirroring topology
--    `principals.omninode_runtime.grants[object_type: SCHEMA, schema: public]`.
--    Idempotent and re-asserted here for the same reason 099 re-asserts the
--    omninode_internal one: a migration must not assume a sibling file ran.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Table grant (topology-derived, OMN-16993)
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON public.session_replay_snapshots TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 4. Assertion: fail the migration if the INSERT grant did not take. Division
--    by zero when the grant is absent -- the same fail-loud shape 099 and
--    node_log_persistence_effect/0000 already use.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_runtime_session_replay_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name = 'session_replay_snapshots'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';
