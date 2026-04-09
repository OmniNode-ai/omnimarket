# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR lifecycle FSM state model.

Related:
    - OMN-8086: Create pr_lifecycle_state_reducer Node
    - OMN-8070: PR Lifecycle Domain epic
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_event import (
    EnumPrLifecyclePhase,
)


class ModelPrLifecycleEntryFlags(BaseModel):
    """Entry flags that control which transitions are enabled.

    These flags are set at sweep start and remain immutable through the lifecycle.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool = Field(
        default=False,
        description="If true, all transitions allowed but no side-effect intents emitted.",
    )
    inventory_only: bool = Field(
        default=False,
        description="If true, only IDLE -> INVENTORYING transition is allowed; stops after triage.",
    )
    fix_only: bool = Field(
        default=False,
        description="If true, only TRIAGED -> FIXING transition is allowed; skips merge.",
    )


class ModelPrLifecycleState(BaseModel):
    """Frozen FSM state for the PR lifecycle sweep.

    The reducer is the ONLY authority that produces new instances of this model.
    All phase transitions go through the reducer's delta function.

    Entry flags (dry_run, inventory_only, fix_only) control transition availability.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Unique ID for this PR lifecycle sweep."
    )
    phase: EnumPrLifecyclePhase = Field(
        default=EnumPrLifecyclePhase.IDLE,
        description="Current FSM phase.",
    )
    entry_flags: ModelPrLifecycleEntryFlags = Field(
        default_factory=ModelPrLifecycleEntryFlags,
        description="Entry flags controlling which transitions are enabled.",
    )
    started_at: datetime | None = Field(
        default=None, description="Timestamp when the sweep started."
    )
    last_phase_at: datetime | None = Field(
        default=None, description="Timestamp of the last phase transition."
    )
    error_message: str | None = Field(
        default=None, description="Error message if phase is FAILED."
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
    prs_processed: int = Field(
        default=0, ge=0, description="Total PRs processed in this sweep cycle."
    )


__all__: list[str] = ["ModelPrLifecycleEntryFlags", "ModelPrLifecycleState"]
