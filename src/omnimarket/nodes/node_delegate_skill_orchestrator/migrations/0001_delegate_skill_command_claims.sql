-- OMN-18887: the durable, correlation-keyed claim on a delegate-skill command.
--
-- Delivery on this path is at-least-once by contract: the consumer runs
-- broker-side auto-commit and never calls commit(), so the fetch position
-- advances ahead of in-flight handlers, and since OMN-18852 four records run
-- in flight at once. A rebalance, a crash or a rewind therefore re-ran a
-- delegation end to end -- a fresh inference, a second provider call, a second
-- billing row -- and nothing noticed, because the node declared
-- descriptor.idempotent false and dispatched with no lookup of any kind.
--
-- This table is CONTROL state, deliberately separate from delegation_events,
-- which is EVIDENCE. Gating a billing decision on the analytics projection
-- would invert the dependency and, worse, that row is written downstream and
-- does not exist yet when the command is handled.
--
-- The claim is atomic by construction. `claimed_at` is written on INSERT and
-- never on conflict, so the value returned to a caller is the FIRST claimer's;
-- comparing it against the value that caller passed is the "did I win" answer
-- in one statement, with no read-then-act window for four concurrent records
-- to slip through.
--
-- `terminal_json` is how a suppressed redelivery still ANSWERS. Returning
-- nothing would publish no terminal at all and convert a double-bill into the
-- missing-envelope defect OMN-15504 exists to prevent.

-- Schema-qualified, and not by preference. The application-domain gate
-- refuses an unqualified application relation outright, because an
-- unqualified CREATE lands wherever search_path happens to point and that is
-- how a control table ends up in public on one deployment and not another.
-- No CREATE SCHEMA here: omninode_internal already exists, and creating it
-- would be the node asserting ownership of a shared namespace.
CREATE TABLE IF NOT EXISTS omninode_internal.delegate_skill_command_claims (
    -- The DELIVERING RECORD's identity, not the correlation. Correlation is
    -- the retry identity by construction: it defaults to a fresh uuid4 but a
    -- caller may supply one, and callers do reuse them. Keyed on correlation,
    -- a reused one would be answered with a stale terminal and never
    -- dispatched -- a worse defect than the double-bill this table prevents.
    -- A redelivery is the same record twice and shares this id; a new command
    -- reusing a correlation is a different record and does not.
    delivery_id    TEXT PRIMARY KEY,
    -- Diagnostics only. This is what lets a reader join a suppressed
    -- redelivery back to the chain it belongs to.
    --
    -- There is deliberately NO tenant_id. An omninode_internal relation
    -- receives no tenant stamping and no row-level security, so a tenant
    -- column here would be a posture the schema cannot enforce -- which the
    -- OMN-18774 gate refuses, and rightly: a tenant column nothing enforces
    -- reads like isolation and provides none. The claim does not need one; it
    -- keys on the delivering record, which is tenant-agnostic.
    correlation_id TEXT NOT NULL DEFAULT '',
    claimed_at     TEXT NOT NULL,
    terminal_json  TEXT NOT NULL DEFAULT ''
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegate_skill_command_claims ----
-- The CREATE TABLE IF NOT EXISTS above SILENTLY NO-OPS when a table of this
-- name already exists with a DIFFERENT shape -- an out-of-band apply, a
-- restored snapshot, a lane that ran an earlier draft of this file. What
-- follows it is not so forgiving: CREATE INDEX IF NOT EXISTS guards the index
-- NAME, not the COLUMN, so it raises
--   ERROR: column "correlation_id" does not exist
-- and ON_ERROR_STOP=1 kills the whole migration Job there. Because the runner
-- halts at the first failure, instances of this class surface strictly one per
-- deploy cycle -- OMN-15376 (llm_cost_aggregates.aggregation_key, deploy-onex-dev
-- run 30418878385) and OMN-15302 (baselines_comparisons.snapshot_id) each cost
-- a whole cycle to discover.
--
-- The guarded adds below converge a drifted pre-existing table onto the shape
-- declared above. On the fresh-create path every one is a no-op, so both paths
-- end at the same set of columns. No DROP, no recreate, no TRUNCATE: the row
-- count of the table this runs against is unknown, and it is not this block's
-- business to reduce it.
--
-- Every add is NULLABLE deliberately, and that is not a weaker copy of the
-- declaration above. A NOT NULL add is refused outright on a drifted table
-- already holding rows, and adding one with a DEFAULT invents a value for rows
-- that never carried the column -- the OMN-16777 precedent is explicit that a
-- guarded add written NOT NULL cannot reconcile a drifted table. The writer is
-- what keeps these columns populated: the claim upsert supplies all four on its
-- INSERT arm, and record_terminal carries claimed_at for the same reason. So
-- the constraint's absence on a repaired table costs nothing the writer does
-- not already guarantee, while its presence would abort the deploy that repairs
-- it.
--
-- delivery_id is reconciled as a plain column. The PRIMARY KEY above is a table
-- constraint rather than a column property, and adding it here would either
-- fail on a drifted table carrying duplicate ids or rewrite the key of a live
-- one. A drifted table that lacks the key is a real defect, but it is a louder
-- and separately-ruled repair than a shape reconciliation may make silently.
--
-- Gated by omnibase_infra tests/ci/test_node_migration_shape_reconciliation.py
-- (static) and tests/integration/migrations/test_node_migration_shape_drift_omn15376.py
-- (execution). The BEGIN/END markers are load-bearing for the second: it derives
-- its pre-fix RED variant by deleting exactly this region. tests/
-- test_omn19029_claims_migration_shape_and_grant.py asserts the same obligation
-- in this repository, where the file is actually edited.
ALTER TABLE omninode_internal.delegate_skill_command_claims
    ADD COLUMN IF NOT EXISTS delivery_id TEXT;
ALTER TABLE omninode_internal.delegate_skill_command_claims
    ADD COLUMN IF NOT EXISTS correlation_id TEXT DEFAULT '';
ALTER TABLE omninode_internal.delegate_skill_command_claims
    ADD COLUMN IF NOT EXISTS claimed_at TEXT;
ALTER TABLE omninode_internal.delegate_skill_command_claims
    ADD COLUMN IF NOT EXISTS terminal_json TEXT DEFAULT '';
-- ---- END OMN-15376 shape reconciliation: delegate_skill_command_claims ----

CREATE INDEX IF NOT EXISTS delegate_skill_command_claims_correlation_idx
    ON omninode_internal.delegate_skill_command_claims (correlation_id);

-- Reclaiming stale rows is deliberately NOT a policy here. A claim that never
-- recorded a terminal is either still running or died mid-flight, and those
-- two are indistinguishable from this table alone. Expiring them on a timer
-- would re-open the double-bill for exactly the slow delegations most worth
-- protecting. The in-flight case is tracked as a follow-up.
