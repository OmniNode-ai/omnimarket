# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_merge_sweep_triage_orchestrator [OMN-8959].

Contains:
- ModelTriageRequest: input to the orchestrator
- ModelAutoMergeArmCommand: emitted for Track A-update PRs that are CLEAN + APPROVED
- ModelRebaseCommand: emitted for PRs that are BEHIND + need rebase
- ModelCiRerunCommand: emitted for PRs that are BLOCKED + checks failing
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
    ModelMergeSweepResult,
)


class ModelTriageRequest(BaseModel):
    """Input to the triage orchestrator. Contains the full classification result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification: ModelMergeSweepResult
    run_id: UUID
    correlation_id: UUID
    total_prs: int = Field(
        default=0,
        description="Total number of PRs in this sweep run (set by orchestrator from len(classified)).",
    )


class ModelAutoMergeArmCommand(BaseModel):
    """Command to arm auto-merge via GraphQL enablePullRequestAutoMerge SQUASH.

    Emitted for PRs: Track A-update, not draft, MERGEABLE, CLEAN, APPROVED, checks passing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    pr_node_id: str  # GitHub GraphQL node ID (PR_kwXXXXXX)
    head_ref_name: str
    correlation_id: UUID
    run_id: UUID
    total_prs: int


class ModelRebaseCommand(BaseModel):
    """Command to rebase a PR branch onto its base.

    Emitted for PRs that are BEHIND main (Track A-update APPROVED+BEHIND, or Track B BEHIND+failing).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    head_ref_name: str
    base_ref_name: str
    head_ref_oid: str  # current SHA at time of emission (for force-with-lease)
    correlation_id: UUID
    run_id: UUID
    total_prs: int


class ModelCiRerunCommand(BaseModel):
    """Command to rerun failed CI checks on a PR.

    Emitted for PRs: Track B, MERGEABLE, BLOCKED, checks failing (stale-CI hypothesis).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    run_id_github: str  # GitHub Actions run ID (resolved from statusCheckRollup)
    correlation_id: UUID
    run_id: UUID
    total_prs: int
