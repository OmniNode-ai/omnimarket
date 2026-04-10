# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR lifecycle intent model emitted by the reducer.

Related:
    - OMN-8086: Create pr_lifecycle_state_reducer Node
    - OMN-8070: PR Lifecycle Domain epic
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_event import (
    EnumPrLifecyclePhase,
)


class EnumPrLifecycleIntentType(StrEnum):
    """Intent types emitted by the PR lifecycle reducer.

    Each intent type maps to a specific downstream node invocation
    that the orchestrator routes to the appropriate effect or compute node.
    """

    START_INVENTORY = "pr_lifecycle.start_inventory"
    START_REBASE = "pr_lifecycle.start_rebase"
    START_FIX = "pr_lifecycle.start_fix"
    START_MERGE = "pr_lifecycle.start_merge"
    SWEEP_COMPLETE = "pr_lifecycle.sweep_complete"
    SWEEP_FAILED = "pr_lifecycle.sweep_failed"


class ModelPrLifecycleIntent(BaseModel):
    """Intent emitted by the PR lifecycle reducer to drive orchestrator actions.

    The orchestrator consumes these intents and routes them to the
    appropriate effect or compute node.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_type: EnumPrLifecycleIntentType = Field(
        ..., description="The intent type determining which node to invoke."
    )
    correlation_id: UUID = Field(..., description="Sweep correlation ID for tracing.")
    from_phase: EnumPrLifecyclePhase = Field(
        ..., description="Phase that produced this intent."
    )
    payload: dict[str, object] = Field(
        default_factory=dict,
        description="Optional key-value payload for the target node.",
    )


__all__: list[str] = ["EnumPrLifecycleIntentType", "ModelPrLifecycleIntent"]
