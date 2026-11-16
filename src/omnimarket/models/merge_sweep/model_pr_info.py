# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Minimal PR representation for classification (OMN-23854)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPRInfo(BaseModel):
    """Minimal PR representation for classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    title: str
    repo: str
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: str  # BEHIND | BLOCKED | CLEAN | DIRTY | DRAFT | UNKNOWN
    is_draft: bool = False
    review_decision: str | None = None  # APPROVED | CHANGES_REQUESTED | None
    required_checks_pass: bool = True
    required_checks_failed: bool = Field(
        default=False,
        description=(
            "At least one required status context has a terminal failure conclusion. "
            "Pending or queued checks are not failures."
        ),
    )
    required_checks_pending: bool = Field(
        default=False,
        description=(
            "At least one required status context is queued, pending, in progress, "
            "or otherwise missing a terminal conclusion."
        ),
    )
    labels: list[str] = Field(default_factory=list)
    review_bot_gate_passed: bool | None = Field(
        default=None,
        description=(
            "State of the review-bot/all-findings-resolved commit status. "
            "None = not yet set (treated as pending). "
            "True = all findings resolved by bot. False = findings still open."
        ),
    )
    required_approving_review_count: int | None = Field(
        default=None,
        description=(
            "Branch-protection required approving review count for the base branch. "
            "0 or None means protection does not require approval and reviewDecision "
            "may be blank on solo-dev repos; >0 means an APPROVED review is required "
            "before merge-sweep may enqueue. Populated per-repo per-sweep from "
            "`gh api repos/.../branches/main/protection`. [OMN-9106]"
        ),
    )
    head_sha: str | None = Field(
        default=None,
        description=(
            "Current HEAD commit SHA from headRefOid. "
            "Used by stall detector to distinguish a force-push (new sha = not stalled) "
            "from a genuinely frozen PR (same sha across consecutive snapshots). [OMN-9404]"
        ),
    )
    pr_node_id: str | None = Field(
        default=None,
        description=(
            "GitHub GraphQL node ID for the pull request. "
            "Lets the triage orchestrator emit auto-merge commands without "
            "performing its own GitHub lookup."
        ),
    )
    head_ref_name: str | None = Field(
        default=None,
        description=(
            "GitHub head ref name for this PR. "
            "Lets the triage orchestrator emit rebase/auto-merge commands "
            "without performing its own GitHub lookup."
        ),
    )
    base_ref_name: str | None = Field(
        default=None,
        description=(
            "GitHub base ref name for this PR. "
            "Lets the triage orchestrator emit rebase/conflict commands "
            "without performing its own GitHub lookup."
        ),
    )
    failing_run_id_github: str | None = Field(
        default=None,
        description=(
            "Most recent failing GitHub Actions run ID derived from status checks. "
            "Lets the triage orchestrator emit CI rerun/fix commands without "
            "performing its own GitHub lookup."
        ),
    )
    failing_job_name: str | None = Field(
        default=None,
        description=(
            "First failing check/job name derived from status checks. "
            "Lets the triage orchestrator emit CI fix commands without "
            "performing its own GitHub lookup."
        ),
    )
    open_thread_comment_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Open review-thread comment GraphQL IDs. "
            "Lets the triage orchestrator emit thread-reply commands without "
            "performing its own GitHub lookup."
        ),
    )
    changed_file_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Changed file paths for the PR. "
            "Lets the triage orchestrator emit conflict-hunk commands without "
            "performing its own GitHub lookup."
        ),
    )


__all__ = ["ModelPRInfo"]
