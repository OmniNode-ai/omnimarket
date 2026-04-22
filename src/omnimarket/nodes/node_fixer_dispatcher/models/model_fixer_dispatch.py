# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_fixer_dispatcher."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EnumStallCategory(str):
    """Stall categories that map to fixer nodes."""

    RED = "red"  # CI failing
    CONFLICTED = "conflicted"  # merge conflict
    BEHIND = "behind"  # needs rebase
    DEPLOY_GATE = "deploy_gate"  # deploy gate blocking
    UNKNOWN = "unknown"  # no auto-fix available
    STALE = "stale"  # old ticket, no activity


class EnumFixerAction(str):
    """Actions the dispatcher can prescribe."""

    DISPATCH_CI_FIX = "dispatch_ci_fix"
    DISPATCH_CONFLICT_RESOLVE = "dispatch_conflict_resolve"
    DISPATCH_REBASE = "dispatch_rebase"
    DISPATCH_DEPLOY_GATE_SKIP = "dispatch_deploy_gate_skip"
    ESCALATE = "escalate"
    NOOP = "noop"


class ModelFixerDispatchRequest(BaseModel):
    """Input: a PR stall event with context for fixer routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(description="GitHub PR number.")
    repo: str = Field(description="GitHub repo slug (e.g. 'omnimarket').")
    stall_category: str = Field(
        description=(
            "Stall category from triage: red, conflicted, behind, deploy_gate, "
            "unknown, stale."
        ),
    )
    blocking_reason: str = Field(
        default="",
        description="Human-readable blocking reason from stall detector.",
    )
    stall_count: int = Field(
        default=1,
        ge=1,
        description="Number of consecutive identical snapshots.",
    )
    head_sha: str | None = Field(
        default=None,
        description="HEAD SHA at time of stall detection.",
    )
    branch_name: str = Field(
        default="",
        description="Branch name for the PR, used by rebase effect.",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, return dispatch spec without executing.",
    )


class ModelFixerDispatchResult(BaseModel):
    """Output: dispatch spec for the correct fixer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    action: str = Field(description="The prescribed fixer action.")
    target_node: str = Field(
        default="",
        description="Entry point name of the target fixer node (empty for escalate/noop).",
    )
    target_topic: str = Field(
        default="",
        description="Command topic to publish for the target fixer.",
    )
    payload_hint: dict[str, str] = Field(
        default_factory=dict,
        description="Pre-filled payload fields for the fixer command.",
    )
    reason: str = Field(
        description="Human-readable explanation of the routing decision.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Routing confidence (1.0 = mechanical, <0.5 = heuristic).",
    )
