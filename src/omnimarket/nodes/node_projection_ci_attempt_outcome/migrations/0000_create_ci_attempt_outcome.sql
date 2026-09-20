-- =============================================================================
-- MIGRATION: per-attempt continuous-integration outcome read model
-- =============================================================================
-- Ticket:  OMN-18903 (decision 2 of epic OMN-18850, the decision-workflow eval)
-- Owner:   omnimarket.nodes.node_projection_ci_attempt_outcome
-- Version: 1.0.0
--
-- WHY THIS EXISTS
--   The cause of a failed continuous-integration attempt is computed and then
--   thrown away. The merge-check reason-code classifier is deterministic and
--   network-free and it runs on every failed check, but its verdict lands on
--   an in-memory field of the pull-request inventory node's output model and
--   reaches no table. Nothing records, per attempt, which head commit failed,
--   which check failed on it, which attempt of that run it was, or why.
--
--   The consequence measured on 2026-09-20: the attempt count the whole
--   decision-workflow eval metric rests on can only be reconstructed by
--   re-walking the code host's runs interface for every pull request every
--   time anyone asks, and the cause split -- 118 of 141 failing steps being a
--   governance gate refusing a missing evidence artifact -- exists only inside
--   a one-off report. A number with no surface a second reading can disagree
--   with is not evidence.
--
-- WHY THE KEY IS FIVE COLUMNS
--   (repository, pr_number, head_sha, check_name, run_attempt) is the finest
--   grain at which the code host reports an outcome, and every coarser key
--   loses something the metric needs:
--
--     drop run_attempt  -- and the same-commit re-run rescue becomes
--                          inexpressible. 154 of 1,635 measured runs went
--                          green only on a LATER attempt of an unchanged
--                          commit; that is a statement about two attempts and
--                          a table holding one row per check cannot make it.
--     drop head_sha     -- and the attempt count, which IS the count of
--                          distinct head commits, cannot be derived at all.
--     drop check_name   -- and a pull request whose lint failed and whose
--                          evidence gate refused collapses to one cause.
--     drop repository   -- and pull-request numbers from four repositories
--                          collide.
--
--   This table therefore APPENDS one row per (check, attempt) rather than
--   replacing per pull request. It is bounded by real continuous-integration
--   volume, roughly 307 merged pull requests a week at a measured 42-60
--   workflow runs per push, and the rows are the history the metric reads.
--
-- WHY attempt_ordinal IS STORED RATHER THAN DERIVED
--   The ordinal is the position of head_sha among the pull request's own head
--   commits, in commit order: 1, 2, 3. It could be computed at read time with
--   a window function over first_seen_at, and that computation would be WRONG
--   whenever rows arrive out of order -- a redelivered message, a backfill, a
--   consumer restarted mid-partition. The producer knows the true commit
--   order; the reader does not. Storing it keeps the ordinal a fact about the
--   pull request instead of a fact about when the projection happened to run.
--
-- WHY ticket_id IS NULLABLE AND NEVER GUESSED
--   It is parsed from the pull-request title. 2 of the 307 pull requests
--   measured over the seven days to 2026-09-20 carry no ticket in the title,
--   and a row whose ticket cannot be parsed is written with NULL and counted
--   in a named unattributed counter. It is never dropped, because a dropped
--   row makes the denominator wrong silently, and never given a guessed
--   ticket, because a guessed ticket makes a work unit's attempt count wrong
--   in a way nothing can detect afterwards.
--
-- WHY cause_affirmative IS A COLUMN AND NOT AN INFERENCE FROM cause_code
--   The classifier fails closed: an unrecognised failure is recorded as
--   runner infrastructure, never as a product failure, deliberately. That
--   makes the code alone unable to distinguish an environment fault somebody
--   actually identified from one nothing recognised. Both are
--   'runner_infra'. The eval's stopping rule is written on the second of
--   those -- it ends the experiment when the unrecognised share exceeds 15
--   percent -- so a share computed over codes is zero by construction and
--   tells nobody anything. OMN-18902 made the provenance a reported fact and
--   this column is where it lands.
--
-- WHY THE CHECK CONSTRAINT LISTS SIX VALUES
--   The six members of the merge-check reason-code vocabulary as of
--   OMN-18902. A seventh member added later fails an INSERT here rather than
--   landing an unconstrained string, which is the intended direction: a row
--   carrying a cause nothing in this schema knows about is worse than a
--   refused write, because it reads as data.
--
-- TWO DECLARATIONS THIS MIGRATION DEPENDS ON, NEITHER OF THEM IN THIS FILE
--   Found the hard way, recorded so the next vendoring does not rediscover it.
--
--   1. The OMN-15361 SQL ownership gate resolves a created application object
--      against omnimarket's scripts/application-relation-ownership.yaml, on
--      the omnimarket ref it checks out -- NOT against the owning node's
--      db_io block and NOT against this repo. An object with no authoritative
--      declaration there is refused. The SEQUENCE behind the BIGSERIAL cursor
--      needs its OWN entry: the gate treats a sequence named by a GRANT as an
--      application target in its own right, for the same reason a table GRANT
--      does not reach it.
--
--   2. Which omnimarket ref gets checked out is decided by the
--      Node-Migration-Source-* trailers in the PULL REQUEST BODY, not in any
--      commit message. scripts/resolve_node_migration_source_ref.py reads the
--      event payload, requires the PR and SHA trailers together, and validates
--      both against the live source pull request head.
--
-- Idempotency: CREATE TABLE IF NOT EXISTS plus one guarded ADD COLUMN per
-- declared column, so re-running converges the SHAPE and not merely the
-- existence. Nothing here touches RLS, ownership or any role attribute.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.ci_attempt_outcome (
    repository         TEXT        NOT NULL,
    pr_number          INTEGER     NOT NULL,
    head_sha           TEXT        NOT NULL,
    check_name         TEXT        NOT NULL,
    run_attempt        INTEGER     NOT NULL,

    cause_code         TEXT        NOT NULL,
    cause_affirmative  BOOLEAN     NOT NULL,
    attempt_ordinal    INTEGER     NOT NULL,

    ticket_id          TEXT,
    failed_step_name   TEXT,
    run_id             TEXT,
    job_conclusion     TEXT,

    observed_at        TIMESTAMPTZ NOT NULL,
    first_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor  BIGSERIAL   NOT NULL,

    CONSTRAINT pk_ci_attempt_outcome
        PRIMARY KEY (repository, pr_number, head_sha, check_name, run_attempt),
    CONSTRAINT ck_ci_attempt_outcome_cause_code
        CHECK (cause_code IN (
            'stale_context',
            'github_api_outage',
            'runner_infra',
            'process_gate_refused',
            'cancelled',
            'product_failed'
        )),
    CONSTRAINT ck_ci_attempt_outcome_attempt_ordinal_positive
        CHECK (attempt_ordinal >= 1),
    CONSTRAINT ck_ci_attempt_outcome_run_attempt_positive
        CHECK (run_attempt >= 1)
);

-- COLUMN RECONCILIATION: one guarded ADD COLUMN per declared column, so
-- CREATE TABLE IF NOT EXISTS stays idempotent in SHAPE, not just existence.
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS repository        TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS pr_number         INTEGER;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS head_sha          TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS check_name        TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS run_attempt       INTEGER;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS cause_code        TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS cause_affirmative BOOLEAN;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS attempt_ordinal   INTEGER;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS ticket_id         TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS failed_step_name  TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS run_id            TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS job_conclusion    TEXT;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS observed_at       TIMESTAMPTZ;
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS first_seen_at     TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS updated_at        TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.ci_attempt_outcome
    ADD COLUMN IF NOT EXISTS projection_cursor BIGSERIAL;

-- The projection cursor the snapshot exposure pages on.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ci_attempt_outcome_projection_cursor
    ON omninode_internal.ci_attempt_outcome (projection_cursor);

-- The metric's own access path: attempts per work unit, in order.
CREATE INDEX IF NOT EXISTS idx_ci_attempt_outcome_ticket
    ON omninode_internal.ci_attempt_outcome (ticket_id)
    WHERE ticket_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ci_attempt_outcome_pr_ordinal
    ON omninode_internal.ci_attempt_outcome
       (repository, pr_number, attempt_ordinal);

-- The cause split, which is the reported figure.
CREATE INDEX IF NOT EXISTS idx_ci_attempt_outcome_cause
    ON omninode_internal.ci_attempt_outcome (cause_code, cause_affirmative);

CREATE INDEX IF NOT EXISTS idx_ci_attempt_outcome_observed_at
    ON omninode_internal.ci_attempt_outcome (observed_at DESC);

COMMENT ON TABLE omninode_internal.ci_attempt_outcome IS
    'OMN-18903: one row per (repository, pull request, head commit, check, run attempt) '
    'carrying the OMN-18902 merge-check cause code, whether that code was reached '
    'affirmatively or by failing closed, and the attempt ordinal within the pull request. '
    'Written only by node_projection_ci_attempt_outcome. A row whose ticket cannot be parsed '
    'from the pull-request title carries a NULL ticket and is counted, never dropped and '
    'never guessed.';

COMMENT ON COLUMN omninode_internal.ci_attempt_outcome.cause_affirmative IS
    'FALSE means the classifier reached cause_code by failing closed rather than by '
    'recognising the failure. The eval reports these as unrecognised; the code alone '
    'cannot express the distinction, which is why this is a column.';

COMMENT ON COLUMN omninode_internal.ci_attempt_outcome.attempt_ordinal IS
    'Position of head_sha among the pull request''s head commits, in commit order, from 1. '
    'Stored rather than derived at read time: the producer knows the true commit order and '
    'a window function over arrival time is wrong whenever rows arrive out of order.';
