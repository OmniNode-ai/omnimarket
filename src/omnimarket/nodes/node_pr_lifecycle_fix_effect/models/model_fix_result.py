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


class ModelOccCompanionVerification(BaseModel):
    """Independent read-back proof that an OCC Evidence-Source companion landed.

    OMN-14173: the ``receipt_evidence_source_autobind`` arm previously reported
    ``fix_applied=True`` whenever the adapter call returned without raising —
    including the no-op / short-circuit paths that pushed nothing. That produced
    a false ``prs_fixed`` count with zero authored companions. This model carries
    the *verified effect* (not the call): a companion is proven only when the
    product PR body carries ``Evidence-Source: OCC#<n>``, that OCC PR is OPEN,
    and the expected ``auto/*`` companion branch exists on the OCC remote. All
    three must hold; the verifier fails CLOSED (``verified=False``) on any
    missing evidence, resolution error, or unwired verifier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: bool = Field(
        ...,
        description=(
            "True only when the pushed OCC companion + Evidence-Source patch "
            "are independently confirmed by a GitHub read-back. Fails closed."
        ),
    )
    occ_pr_number: int | None = Field(
        default=None,
        description="OCC companion PR number read from the product PR body.",
    )
    occ_branch: str | None = Field(
        default=None,
        description="Expected auto/* OCC companion branch that was probed.",
    )
    evidence_source_present: bool = Field(
        default=False,
        description="Product PR body carries `Evidence-Source: OCC#<n>`.",
    )
    occ_pr_open: bool = Field(
        default=False,
        description="The referenced OCC companion PR is in the open state.",
    )
    branch_exists: bool = Field(
        default=False,
        description="The auto/* companion branch exists on the OCC remote.",
    )
    detail: str = Field(
        default="",
        description="Human-readable verification detail (reason on failure).",
    )


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
    occ_companion_verified: bool = Field(
        default=False,
        description=(
            "OMN-14173 fail-closed accounting: True ONLY when this is an "
            "OCC-evidence arm (receipt_evidence_source_autobind) whose pushed "
            "OCC companion + Evidence-Source patch were independently verified "
            "by a GitHub read-back. Always False for non-OCC arms and for a "
            "classified-but-not-minted / no-op / short-circuited OCC dispatch. "
            "The orchestrator gates `prs_fixed` on this flag for the autobind "
            "arm so a false-success (fix_applied=True, zero companions) can "
            "never be counted."
        ),
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


__all__: list[str] = [
    "EnumDelegationOutcome",
    "ModelOccCompanionVerification",
    "ModelPrLifecycleFixResult",
]
