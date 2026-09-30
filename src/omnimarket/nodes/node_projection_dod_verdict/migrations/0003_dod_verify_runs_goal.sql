-- SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
--
-- Migration 0003: bind each immutable DoD verification attempt to the goal
-- contract revision it evaluated (OMN-20025 / GC.2). Existing ticket runs keep
-- NULL goal identity and remain readable with their original key.

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS goal_id UUID;

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS parent_goal_id UUID;

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS level TEXT
        CHECK (level IS NULL OR level IN ('delegate_call', 'workflow_lane', 'interactive_session'));

ALTER TABLE omninode_internal.dod_verify_runs
    ADD COLUMN IF NOT EXISTS contract_revision UUID;

CREATE INDEX IF NOT EXISTS idx_dod_verify_runs_goal_revision
    ON omninode_internal.dod_verify_runs (goal_id, contract_revision)
    WHERE goal_id IS NOT NULL;
