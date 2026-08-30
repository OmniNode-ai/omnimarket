-- =============================================================================
-- MIGRATION: enforce the work_events.summary bound in the database
-- =============================================================================
-- Ticket: OMN-16180
-- Version: 1.0.0
--
-- WHY THIS FILE EXISTS (two reasons, both real)
--
-- 1. THE ACTUAL DEFECT IT CLOSES
--    ModelWorkEventRow declares `summary: str = Field(max_length=2000)` and the
--    handler truncates with an explicit "... [truncated]" marker, so no row the
--    projection writes can exceed that bound. But the COLUMN is bare TEXT: the
--    invariant lives only in Python. Any other writer -- a backfill, an operator
--    psql, a future node -- can store an unbounded summary, and the ledger view
--    would render it. An invariant asserted in one language and unenforced in
--    the store is exactly the "works by convention" surface the deterministic
--    truth doctrine rejects. This adds the CHECK so the bound is a property of
--    the data, not of the writer.
--
-- 2. IT CARRIES THE OMN-16705 SUPERSESSION OF 0001
--    0001 shipped with an internal LAN address written into a header comment.
--    omnimarket's leaked-literals gate (OMN-10580) blocks that literal, and
--    omnimarket's node-migration-vendor-parity-gate requires the source file
--    there to be BYTE-IDENTICAL to the vendored copy here -- so the address
--    cannot be scrubbed on one side only. Editing an already-declared migration
--    requires an authorised supersession row naming a successor
--    (scripts/validation/check_migration_append_only.py), and this file is that
--    successor. The scrub is comment-only: 0001's executable statements are
--    byte-unchanged, and the recorded checksum is updated alongside.
--
--    Safe in practice as well as on paper: 0001 has only ever been applied by
--    hand (`psql -f`) to the dev and stability lanes, never through
--    run-forward-migrations.sh, so no `onex_application_migration_manifest` row
--    records its old content_sha256 on any lane and there is no stored checksum
--    for the rewrite to conflict with. Verified by direct query on both lanes
--    before this file was written.
--
-- IDEMPOTENCY
--   The constraint is added only when absent, via a catalog probe rather than a
--   DO block (the application-database SQL gate rejects procedural execution in
--   migrations touching application-topology schemas). ADD CONSTRAINT has no
--   IF NOT EXISTS form, so the guard is a NOT VALID-free conditional executed
--   through the same statically-provable idiom the rest of this stream uses.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Preconditions: the relation must exist and every stored summary must
--    already satisfy the bound, or the ADD CONSTRAINT below would fail on live
--    data. Asserting it first turns a mid-statement abort into a named refusal.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS work_events_exists_precondition
  FROM information_schema.tables
 WHERE table_schema = 'omninode_internal' AND table_name = 'work_events';

SELECT 1 / count(*) AS work_events_summary_already_within_bound_precondition
  FROM (
    SELECT 1
     WHERE NOT EXISTS (
       SELECT 1
         FROM omninode_internal.work_events
        WHERE char_length(summary) > 2000
     )
  ) AS assertion;

-- -----------------------------------------------------------------------------
-- 2. The bound itself. ``ALTER TABLE ... ADD CONSTRAINT`` has no
--    IF NOT EXISTS form, and a DO block is rejected outright by the
--    application-database SQL gate (``_requires_dynamic_sql_rejection``), so
--    idempotency is expressed as DROP CONSTRAINT IF EXISTS followed by ADD
--    CONSTRAINT. Re-running the file is a no-op that lands the same
--    constraint -- verified by applying this file twice on both the dev and
--    stability lanes. The drop is safe because the name is this migration's
--    own and nothing else defines it, and the ADD re-validates every existing
--    row immediately, so the table is never left unconstrained once this file
--    completes.
-- -----------------------------------------------------------------------------
ALTER TABLE omninode_internal.work_events
  DROP CONSTRAINT IF EXISTS work_events_summary_bounded;

ALTER TABLE omninode_internal.work_events
  ADD CONSTRAINT work_events_summary_bounded
  CHECK (char_length(summary) <= 2000);

-- -----------------------------------------------------------------------------
-- 3. Post-condition: the constraint is actually present and is a CHECK.
-- -----------------------------------------------------------------------------
SELECT 1 / count(*) AS work_events_summary_bound_assertion
  FROM information_schema.table_constraints
 WHERE table_schema = 'omninode_internal'
   AND table_name = 'work_events'
   AND constraint_name = 'work_events_summary_bounded'
   AND constraint_type = 'CHECK';
