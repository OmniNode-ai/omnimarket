-- OMN-14894 (tranche 2): add tenant_id to delegation_judge_verdict_events.
--
-- delegation_judge_verdict_events (migration 0016) carries correlation_id but
-- no tenant identity of its own. HandlerProjectionDelegation.project_judge_verdict
-- writes this table from ModelDelegationJudgeVerdictEvent, which has no
-- tenant_id field either -- the judge-verdict wire event never carried one.
--
-- Live join probe on the stability-test lane (2026-07-26, n=4 rows): 2/4
-- correlation_id values matched a delegation_events row with a real tenant_id
-- ('omninode', 'omn14843-consumer-repro'); the other 2 (same correlation_id
-- value on each side) found no matching delegation_events row. n=4 is far too
-- small to certify a join rate -- the 50% miss on this sample means either
-- late/out-of-order projection (judge verdict lands before the delegation
-- event) or a real correlation-id mismatch. This migration and the writer
-- change below do not resolve that ambiguity; they make the join-then-default
-- behavior explicit and re-verifiable once row volume grows, instead of
-- leaving the table tenant-less.
--
-- tenant_id is populated two ways, matching the pattern already used by
-- delegation_events (0022) and delegation_budget_state (0019):
--   1. This migration backfills existing rows via a one-time correlation_id
--      join to delegation_events.tenant_id.
--   2. HandlerProjectionDelegation.project_judge_verdict (this tranche) does
--      the same join at write time, falling back to DEFAULT_TENANT
--      ('omninode') when no matching delegation_events row exists yet --
--      never a NULL/omitted key (the OMN-14058 writer-erasure pattern this
--      whole tranche exists to close).
--
-- DEFAULT 'omninode' mirrors 0019/0022's interim single-tenant convention.

ALTER TABLE delegation_judge_verdict_events
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode';

-- One-time backfill: pull tenant_id from delegation_events by correlation_id
-- for any row still holding the bare column default. Rows with no matching
-- delegation_events correlation_id keep the 'omninode' default.
UPDATE delegation_judge_verdict_events AS jv
SET tenant_id = de.tenant_id
FROM delegation_events AS de
WHERE jv.correlation_id = de.correlation_id
  AND jv.tenant_id = 'omninode';

CREATE INDEX IF NOT EXISTS idx_delegation_judge_verdict_events_tenant_id
    ON delegation_judge_verdict_events (tenant_id);
