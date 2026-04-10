# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR lifecycle event model — input events consumed by the reducer.

Related:
    - OMN-8086: Create pr_lifecycle_state_reducer Node
    - OMN-8070: PR Lifecycle Domain epic
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumPrLifecyclePhase(StrEnum):
    """FSM phases for the PR lifecycle sweep.

    Phase Transitions:
        IDLE -> INVENTORYING: Sweep command received, collect PR state
        INVENTORYING -> TRIAGED: Inventory complete, classify PRs by block reason
        TRIAGED -> REBASING: Stale (BEHIND/UNKNOWN) PRs found and auto-rebase enabled
        TRIAGED -> FIXING: Blocked PRs found, dispatch fix actions
        TRIAGED -> MERGING: No blocked PRs, proceed to merge eligible
        REBASING -> MERGING: Rebase attempts complete, proceed to merge
        REBASING -> FAILED: Rebase error
        FIXING -> MERGING: Fix actions dispatched, proceed to merge
        MERGING -> COMPLETE: Merge actions dispatched, sweep complete
        Any -> FAILED: Unrecoverable error
    """

    IDLE = "idle"
    INVENTORYING = "inventorying"
    TRIAGED = "triaged"
    REBASING = "rebasing"
    FIXING = "fixing"
    MERGING = "merging"
    COMPLETE = "complete"
    FAILED = "failed"


class EnumPrLifecycleEventTrigger(StrEnum):
    """Trigger names for FSM transitions."""

    START_RECEIVED = "start_received"
    INVENTORY_COMPLETE = "inventory_complete"
    REBASE_PENDING = "rebase_pending"
    REBASE_COMPLETE = "rebase_complete"
    FIXES_PENDING = "fixes_pending"
    NO_FIXES_NEEDED = "no_fixes_needed"
    FIXES_COMPLETE = "fixes_complete"
    MERGE_COMPLETE = "merge_complete"
    ERROR = "error"


class ModelPrLifecycleEvent(BaseModel):
    """Event consumed by the PR lifecycle reducer to drive FSM transitions.

    Each event represents the outcome of a phase's work (success or failure),
    causing the reducer to compute the next state and emit intents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Sweep correlation ID for deduplication."
    )
    source_phase: EnumPrLifecyclePhase = Field(
        ..., description="Phase that produced this event."
    )
    trigger: EnumPrLifecycleEventTrigger = Field(
        ..., description="Transition trigger name."
    )
    success: bool = Field(..., description="Whether the phase completed successfully.")
    timestamp: datetime = Field(..., description="When the event was produced.")
    error_message: str | None = Field(
        default=None, description="Error details if success=False."
    )
    prs_inventoried: int = Field(
        default=0,
        ge=0,
        description="Number of PRs collected during INVENTORYING phase.",
    )
    prs_blocked: int = Field(
        default=0, ge=0, description="Number of blocked PRs found during triage."
    )
    prs_fixed: int = Field(
        default=0, ge=0, description="Number of PRs with fix actions dispatched."
    )
    prs_merged: int = Field(
        default=0, ge=0, description="Number of PRs with merge actions dispatched."
    )


__all__: list[str] = [
    "EnumPrLifecycleEventTrigger",
    "EnumPrLifecyclePhase",
    "ModelPrLifecycleEvent",
]
