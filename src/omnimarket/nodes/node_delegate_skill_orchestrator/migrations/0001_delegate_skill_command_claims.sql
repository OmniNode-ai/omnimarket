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

CREATE TABLE IF NOT EXISTS delegate_skill_command_claims (
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
    correlation_id TEXT NOT NULL DEFAULT '',
    tenant_id      TEXT NOT NULL DEFAULT '',
    claimed_at     TEXT NOT NULL,
    terminal_json  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS delegate_skill_command_claims_correlation_idx
    ON delegate_skill_command_claims (correlation_id);

-- Reclaiming stale rows is deliberately NOT a policy here. A claim that never
-- recorded a terminal is either still running or died mid-flight, and those
-- two are indistinguishable from this table alone. Expiring them on a timer
-- would re-open the double-bill for exactly the slow delegations most worth
-- protecting. The in-flight case is tracked as a follow-up.
