# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build loop intent model emitted by the reducer.

Migrated from omnibase_infra [OMN-7577].

Related:
    - OMN-7311: ModelBuildLoopState foundation models
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_event import (
    EnumBuildLoopPhase,
)


class EnumBuildLoopIntentType(StrEnum):
    """Intent types emitted by the build loop reducer.

    Each intent type maps to a specific downstream node invocation
    that the orchestrator routes to the appropriate effect or compute node.
    """

    START_CLOSEOUT = "build_loop.start_closeout"
    START_VERIFY = "build_loop.start_verify"
    START_FILL = "build_loop.start_fill"
    START_CLASSIFY = "build_loop.start_classify"
    START_BUILD = "build_loop.start_build"
    CYCLE_COMPLETE = "build_loop.cycle_complete"
    CIRCUIT_BREAK = "build_loop.circuit_break"


class ModelBuildLoopIntent(BaseModel):
    """Intent emitted by the build loop reducer to drive orchestrator actions.

    The orchestrator consumes these intents and routes them to the
    appropriate effect or compute node.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent_type: EnumBuildLoopIntentType = Field(
        ..., description="The intent type determining which node to invoke."
    )
    correlation_id: UUID = Field(..., description="Cycle correlation ID for tracing.")
    cycle_number: int = Field(..., ge=0, description="Current cycle number.")
    from_phase: EnumBuildLoopPhase = Field(
        ..., description="Phase that produced this intent."
    )
    payload: dict[str, object] = Field(
        default_factory=dict,
        description="Optional key-value payload for the target node.",
    )


__all__: list[str] = ["EnumBuildLoopIntentType", "ModelBuildLoopIntent"]
