-- OMN-17379: topology-derived omninode_runtime grant for pr_merged_events,
-- INCLUDING the sequence its BIGSERIAL primary key drives.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_pr_merged_projection
--
-- WHY THIS EXISTS -- the table grant alone was never sufficient
--
--   `0001_create_pr_merged_events.sql` declares
--
--       projection_cursor BIGSERIAL PRIMARY KEY
--
--   A BIGSERIAL column is a plain `nextval()` DEFAULT over a standalone
--   sequence. Postgres checks that sequence's OWN acl on every INSERT, and
--   `GRANT INSERT ON TABLE` does not reach it. (An identity column --
--   `GENERATED ... AS IDENTITY` -- is the opposite: its sequence is owned by
--   the column and the table's INSERT privilege covers it. That is why the
--   sibling grant migrations for tables without a SERIAL key, e.g.
--   `node_projection_session_replay/0002` for `session_replay_snapshots`, were
--   complete without this half and this one is not.)
--
--   So the shipped grant landed exactly half the privilege the write needs.
--   Verified live on the .201 dev lane 2026-08-31:
--
--       information_schema.role_table_grants
--         -> omninode_runtime: INSERT, SELECT, UPDATE on pr_merged_events   (present)
--
--       pg_class.relacl for pr_merged_events_projection_cursor_seq
--         -> {postgres=rwU/postgres, role_omnidash=rU/postgres}             (absent)
--
--   `has_sequence_privilege('omninode_runtime', ..., 'USAGE')` returned FALSE.
--
-- CONSEQUENCE, observed live -- this is not a hypothetical
--
--   The identity re-point that stranded this table was THIS repo's own
--   `feat(OMN-15423): classify application relation ownership` (omnimarket
--   #1956, merged 2026-07-31), which rewrote this contract's `db_io` from
--
--       database: omnidash_analytics      -> _DB_URL_ENV_MAP -> OMNIDASH_ANALYTICS_DB_URL
--   to
--       database_ref: application         -> topology binding omninode_runtime_service
--                                         -> OMNINODE_INTERNAL_DB_URL
--
--   Those two DSNs address the same database as DIFFERENT principals, verified
--   live in `omninode-runtime-effects` on 2026-08-31:
--
--       OMNIDASH_ANALYTICS_DB_URL = postgresql://role_omnidash@postgres/omnidash_analytics
--       OMNINODE_INTERNAL_DB_URL  = postgresql://omninode_runtime@postgres/omnidash_analytics
--
--   `role_omnidash` carries the blanket
--   `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public` from omnibase_infra
--   migration 096, so the pre-#1956 write path held sequence USAGE by
--   inheritance and nobody had to think about it. `omninode_runtime` carries no
--   sequence grant anywhere in either repo. The switch therefore silently
--   dropped half the privilege the INSERT needs. Newest row before this fix:
--   PR #2015, merged 2026-08-03 -- three days after #1956.
--
--   NOT the cause, though it is where the failure became OBSERVABLE: OMN-16843
--   (omnibase_infra #2968, merged 2026-08-28) wired `OMNINODE_INTERNAL_DB_URL`
--   into the compose lanes, which no compose file had set before. Until that
--   landed, a `database_ref: application` projection could not resolve a DSN and
--   never attached at all. From 2026-08-28 this one attached, consumed
--   offsets 63..96, and failed EVERY write with
--
--       InsufficientPrivilege: permission denied for sequence
--       pr_merged_events_projection_cursor_seq
--
--   while the runtime swallowed the error, quarantined the record and committed
--   the offset anyway. So the 24-day hole has two phases -- unattached
--   (08-03 -> 08-28), then attached-and-discarding (08-28 -> 08-31) -- and both
--   present the identical external signature: consumer Stable, MEMBERS 1,
--   TOTAL-LAG 0, table frozen.
--
--   The second phase is the one proven directly rather than reconstructed:
--   rewinding the group to offset 94 and re-consuming 94..96 on the real wired
--   path produced three errors, three quarantine records, and zero rows.
--   `pr_merged_events` held 28 rows while omnimarket merged through #2249.
--
--   The swallow itself is fixed separately in omnibase_infra (OMN-17379,
--   `ProjectionNotMaterializedError`) so that a future grant gap STALLS the feed
--   with visible lag instead of running green. This file fixes the privilege.
--
-- SCOPE
--   This file grants ONLY this node's own relation and its own sequence, exactly
--   as `node_projection_session_replay/0002` (OMN-16993) does for its table. The
--   same sequence-half gap exists for every other SERIAL/BIGSERIAL-keyed relation
--   in the same topology grant list -- 12 sequences in `public` return FALSE for
--   `has_sequence_privilege('omninode_runtime', ..., 'USAGE')`, including
--   `merge_state_transitions`, `pr_lifecycle_ledger_entries` and
--   `receipt_gate_rows`, all three of which are sitting at ZERO rows for this
--   reason. Each belongs in its own node's migration.
--
-- PRIVILEGES
--   Table: SELECT, INSERT, UPDATE and no DELETE -- a projection writer upserts,
--   it does not reshape the table. Sequence: USAGE, which permits `nextval()`
--   and `currval()` and nothing else. NOT `UPDATE`, which would additionally
--   permit `setval()` -- the projection cursor's strict monotonicity is the
--   contract the reaper's `?since=<cursor>` read depends on, and a writer that
--   can rewind the sequence can silently violate it.
--
-- Idempotency: GRANT is idempotent; re-running is a no-op.

-- ---------------------------------------------------------------------------
-- 1. Schema USAGE, mirroring topology
--    `principals.omninode_runtime.grants[object_type: SCHEMA, schema: public]`.
--    Idempotent and re-asserted here for the same reason 099 re-asserts the
--    omninode_internal one: a migration must not assume a sibling file ran.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 2. Table grant (topology-derived)
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON public.pr_merged_events TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Sequence grant -- the half that was missing (OMN-17379)
--    Resolved through `pg_get_serial_sequence` rather than by spelling the
--    sequence name, so a table whose sequence was created under a different
--    name (a restore, a rename, an out-of-band apply) still converges. A NULL
--    return means the column is not sequence-backed at all, which would
--    contradict `0001`'s BIGSERIAL declaration -- fail loud rather than
--    no-op into another silent half-grant.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_seq TEXT;
BEGIN
    v_seq := pg_get_serial_sequence('public.pr_merged_events', 'projection_cursor');
    IF v_seq IS NULL THEN
        RAISE EXCEPTION
            'OMN-17379: public.pr_merged_events.projection_cursor is not backed by a sequence, but 0001 declares it BIGSERIAL. Refusing to grant a privilege on an object that does not exist -- reconcile the column shape first.';
    END IF;
    EXECUTE format('GRANT USAGE ON SEQUENCE %s TO omninode_runtime', v_seq);
END$$;

-- ---------------------------------------------------------------------------
-- 4. Assertions: fail the migration if either half did not take. Division by
--    zero when the grant is absent -- the same fail-loud shape 099 and
--    node_log_persistence_effect/0000 already use.
--
--    The SECOND assertion is the one this ticket exists for. Asserting only the
--    table INSERT is exactly what let the broken state ship: it was TRUE for the
--    whole 24-day outage.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS omninode_runtime_pr_merged_events_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name = 'pr_merged_events'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS omninode_runtime_pr_merged_events_sequence_usage_assertion
WHERE has_sequence_privilege(
          'omninode_runtime',
          pg_get_serial_sequence('public.pr_merged_events', 'projection_cursor'),
          'USAGE'
      );
