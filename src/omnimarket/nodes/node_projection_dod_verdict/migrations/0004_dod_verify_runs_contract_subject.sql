-- SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
--
-- Migration 0004: record the contract each DoD verdict was evaluated against
-- (OMN-20696, split from OR.3 OMN-20071, OCC retirement plan step S4).
--
-- WHY: the Done gate takes a ticket's verdict from the repository its PR
-- merged into, read through the projection access node. A row that cannot say
-- whether its contract came from that repository, and at which commit, cannot
-- answer that read, and cannot be told from a verdict over an OCC contract.
--
-- contract_source is what the verifier resolved when it loaded the file:
-- product_repository or onex_change_control when the file is exactly the
-- content of a commit of a GitHub checkout, inline_goal for an inline goal
-- contract, unbound for anything else (modified, untracked, outside a
-- checkout, a non-GitHub origin). NULL is a verdict that predates this column
-- or was produced from caller-supplied results: not measured.
--
-- The last column carries the cross-column rule inline, so the whole file is
-- additive and idempotent: a repository-owned source must name its
-- repository, commit and path. Existing rows have NULL source and pass.

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS contract_source TEXT
        CHECK (contract_source IS NULL OR contract_source IN
            ('product_repository', 'onex_change_control', 'inline_goal', 'unbound'));

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS contract_repository TEXT;

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS contract_commit_sha TEXT
        CHECK (contract_commit_sha IS NULL
            OR contract_commit_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$');

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS contract_repo_path TEXT
        CONSTRAINT dod_verify_runs_contract_subject_bound
        CHECK (contract_source IS NULL
            OR contract_source NOT IN ('product_repository', 'onex_change_control')
            OR num_nulls(contract_repository, contract_commit_sha, contract_repo_path) = 0);

-- The Done gate's lookup: this ticket's verdicts over this repository's
-- contract at this commit.
CREATE INDEX IF NOT EXISTS idx_dod_verify_runs_contract_subject
    ON omninode_internal.dod_verify_runs (ticket_id, contract_repository, contract_commit_sha)
    WHERE contract_source IS NOT NULL;
