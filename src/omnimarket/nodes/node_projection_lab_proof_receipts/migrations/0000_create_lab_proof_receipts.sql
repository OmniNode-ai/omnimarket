-- OMN-19566 (T2 slice 2 of the lab-proof plan, epic OMN-19564): one row per
-- exact-head PR lab proof.
-- Owner: omnimarket.nodes.node_projection_lab_proof_receipts
-- Target database: omnidash_analytics; physical schema: omninode_internal.
--
-- Key: (repo, pr_number, head_sha, profile_id, profile_version), plan section 3.
-- Re-running the same key replaces the row, guarded on finished_at in the
-- writer's SQL; the history stays on onex.evt.omnibase-infra.lab-proof-receipt.v1.

CREATE TABLE IF NOT EXISTS omninode_internal.lab_proof_receipts (
    repo                      TEXT        NOT NULL,
    pr_number                 INTEGER     NOT NULL,
    head_sha                  TEXT        NOT NULL,
    profile_id                TEXT        NOT NULL,
    profile_version           TEXT        NOT NULL,
    receipt_key               TEXT        NOT NULL,
    handler_kind              TEXT        NOT NULL,
    result                    TEXT        NOT NULL,
    verifier_token            TEXT        NOT NULL,
    verifier_reason           TEXT        NOT NULL,
    mandatory_checks          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    missing_mandatory_checks  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    failing_checks            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    started_at                TIMESTAMPTZ NOT NULL,
    finished_at               TIMESTAMPTZ NOT NULL,
    runner_identity           TEXT        NOT NULL,
    verifier_identity         TEXT        NOT NULL,
    host                      TEXT        NOT NULL,
    slot                      TEXT        NOT NULL,
    carried_from              TEXT        NOT NULL DEFAULT '',
    receipt                   JSONB       NOT NULL,
    projected_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor         BIGSERIAL   NOT NULL,

    CONSTRAINT pk_lab_proof_receipts
        PRIMARY KEY (repo, pr_number, head_sha, profile_id, profile_version),
    CONSTRAINT ck_lab_proof_receipts_result CHECK (result IN ('PASS', 'FAIL')),
    CONSTRAINT ck_lab_proof_receipts_head CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    CONSTRAINT ck_lab_proof_receipts_window CHECK (finished_at >= started_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_proof_receipts_receipt_key
    ON omninode_internal.lab_proof_receipts (receipt_key);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_proof_receipts_projection_cursor
    ON omninode_internal.lab_proof_receipts (projection_cursor);

-- The question a lander and the lab-proof job ask: what proves this PR's head?
CREATE INDEX IF NOT EXISTS idx_lab_proof_receipts_pr_head
    ON omninode_internal.lab_proof_receipts (repo, pr_number, head_sha);

-- The question the proof-bar counter asks: how many distinct PRs has each
-- profile version proved, and has it recorded a negative-control FAIL?
CREATE INDEX IF NOT EXISTS idx_lab_proof_receipts_profile
    ON omninode_internal.lab_proof_receipts (profile_id, profile_version, result);

COMMENT ON TABLE omninode_internal.lab_proof_receipts IS
    'OMN-19566: one row per exact-head PR lab proof, PASS or FAIL, with the '
    'verifier token at mint time. Written only by node_projection_lab_proof_receipts.';
