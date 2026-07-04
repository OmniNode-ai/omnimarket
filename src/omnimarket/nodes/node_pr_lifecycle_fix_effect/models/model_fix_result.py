"""ModelPrLifecycleFixResult — result of a PR lifecycle fix action."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
)


class EnumDelegationOutcome(StrEnum):
    """Terminal delegation outcome for a single fix dispatch (WS-D/D2, OMN-13940).

    accepted      -> delegated fix was attempted and succeeded (gates + verify
                      passed, EXISTING pr_polish flow pushed/completed cleanly).
    gate_failed   -> delegated fix was attempted and failed local gates/verify;
                      no push occurred; not yet the second strike for this
                      PR/block_reason, so no agent fallback was dispatched this
                      call (safety bar #7 — retry or escalate on next tick).
    escalated     -> delegated fix failed and this was the second (permanent)
                      strike for this PR/block_reason — the agent was
                      dispatched as an immediate fallback for this call.
    not_attempted -> eligibility check (denylist, blast-radius, block_reason,
                      or an already-tripped two-strike) refused delegation
                      before any attempt; the agent path was used directly.
    """

    ACCEPTED = "accepted"
    GATE_FAILED = "gate_failed"
    ESCALATED = "escalated"
    NOT_ATTEMPTED = "not_attempted"


class ModelPrLifecycleFixResult(BaseModel):
    """Result of a PR lifecycle fix dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Fix run correlation ID.")
    pr_number: int = Field(..., description="PR number that was remediated.")
    repo: str = Field(..., description="GitHub repo slug.")
    block_reason: EnumPrBlockReason = Field(
        ..., description="Block reason that was routed."
    )
    fix_applied: bool = Field(..., description="Whether a fix action was dispatched.")
    fix_action: str = Field(
        ..., description="Fix action taken or would be taken (dry_run)."
    )
    error: str | None = Field(default=None, description="Error message if fix failed.")
    completed_at: datetime = Field(..., description="When the fix completed.")
    delegated: bool = Field(
        default=False,
        description=(
            "Whether a delegation attempt (non-Claude fix path) was made for "
            "this dispatch, per the WS-D/D2 merge-sweep delegation harness."
        ),
    )
    delegation_model: str | None = Field(
        default=None,
        description=(
            "Model/tool identity that produced the delegated fix, e.g. "
            "'ruff-deterministic' for the Slice 0 zero-LLM harness. None when "
            "delegated is False."
        ),
    )
    delegation_outcome: EnumDelegationOutcome | None = Field(
        default=None,
        description="Terminal delegation outcome; None when delegated is False.",
    )
    delegation_cost_usd: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "USD cost of the delegation attempt. 0.0 for the deterministic "
            "Slice 0 path; populated from HandlerDelegateSkill metrics in "
            "Slice 1. None when delegated is False."
        ),
    )


__all__: list[str] = ["EnumDelegationOutcome", "ModelPrLifecycleFixResult"]
