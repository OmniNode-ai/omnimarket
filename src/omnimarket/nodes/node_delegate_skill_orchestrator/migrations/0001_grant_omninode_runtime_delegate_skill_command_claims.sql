-- =============================================================================
-- MIGRATION: deliver the omninode_runtime grants for delegate_skill_command_claims
-- =============================================================================
-- Ticket:  OMN-19029 (gap 2), under OMN-18887
-- Owner:   omnimarket.nodes.node_delegate_skill_orchestrator
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   `0001_delegate_skill_command_claims.sql` creates the relation; the
--   deployment topology DERIVES the grant from this node's own
--   `db_io.db_tables` entry, which declares the relation `read_write` under
--   the omninode_internal domain and therefore binds it to the
--   `omninode_runtime` projection principal. Declaring a grant is not issuing
--   one. The two halves drift apart silently and the drift is only ever found
--   as a live outage on whichever relation takes traffic next: OMN-17379 is
--   the canonical shape -- every write refused with InsufficientPrivilege
--   while the consumer group reported Stable at LAG 0 and committed its
--   offsets anyway, for 24 days. omnibase_infra's
--   scripts/validation/check_topology_grant_delivery.py is the ratchet that
--   makes this a bound rather than a comment, and it refuses the vendored
--   relation before this file exists:
--     UNDELIVERED  omninode_runtime -> omninode_internal.delegate_skill_command_claims
--
--   A live ACL observed on one lane is not a delivered grant. Nothing replays
--   an out-of-band GRANT onto a fresh database, which is the whole point of
--   keeping it in a migration lineage -- and of keeping it in the OWNING
--   node's lineage rather than a shared cross-node grant file, because a
--   shared file is how a relation added later silently misses its grant
--   (OMN-15701 reverted eight grants at once that way).
--
-- WHY THESE THREE PRIVILEGES AND NO MORE
--   SELECT, INSERT, UPDATE is exactly what the platform derives for a
--   `read_write` declaration, and exactly what this writer needs. The claim is
--   an INSERT ... ON CONFLICT DO UPDATE whose RETURNING clause reads the
--   accepted row back, and that read-back is not diagnostic: it is how the
--   port learns whether the claimed_at it wrote is the one that landed, which
--   is the entire race verdict. A lane where INSERT landed and SELECT did not
--   would answer "did I win" with an exception on every redelivery.
--
--   No DELETE. Nothing in this node retracts a claim. Expiring stale claims on
--   a timer is deliberately not a policy here -- a claim with no recorded
--   terminal is either still running or died mid-flight and this table cannot
--   tell those apart -- so a DELETE privilege would exist only to be misused.
--
--   No sequence grant. The OMN-17447 sequence half exists because a
--   SERIAL/BIGSERIAL key is rewritten into a nextval() DEFAULT over a
--   standalone sequence whose own acl PostgreSQL checks separately, so a table
--   grant alone leaves every INSERT refused. This key is the delivering
--   record's TEXT id, supplied by the caller, so there is no sequence behind
--   it and naming one would fail the migration on an object that does not
--   exist.
--
-- IDEMPOTENCY
--   GRANT is idempotent; re-running is a no-op. Nothing here touches RLS,
--   ownership, or any role attribute. There is no RLS to touch: the relation
--   is omninode_internal control state for one node, declares no tenant
--   column, and the OMN-18774 gate refuses a tenant posture the schema cannot
--   enforce.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Schema USAGE, mirroring the topology's SCHEMA grant. Re-asserted rather
--    than assumed: a migration must not depend on a sibling file having run.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 2. Table grant (topology-derived).
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.delegate_skill_command_claims
    TO omninode_runtime;

-- ---------------------------------------------------------------------------
-- 3. Assertions: fail the migration if a grant did not take. Division by zero
--    when the fact is false -- the fail-loud shape this repository's other
--    grant migrations already use.
--
--    EVERY granted privilege is asserted, not just INSERT. Asserting only the
--    INSERT is exactly what let OMN-17379 ship: it was TRUE for the whole
--    24-day outage while the privilege actually missing was never checked.
-- ---------------------------------------------------------------------------
SELECT 1 / count(*) AS delegate_skill_command_claims_select_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'delegate_skill_command_claims'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'SELECT';

SELECT 1 / count(*) AS delegate_skill_command_claims_insert_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'delegate_skill_command_claims'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'INSERT';

SELECT 1 / count(*) AS delegate_skill_command_claims_update_grant_assertion
FROM information_schema.role_table_grants
WHERE table_schema = 'omninode_internal'
  AND table_name = 'delegate_skill_command_claims'
  AND grantee = 'omninode_runtime'
  AND privilege_type = 'UPDATE';
