# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build loop event model — input events consumed by the reducer.

Migrated from omnibase_infra [OMN-7577].

Related:
    - OMN-7311: ModelBuildLoopState foundation models
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumBuildLoopPhase(StrEnum):
    """FSM phases for the autonomous build loop.

    Phase Transitions:
        IDLE -> CLOSING_OUT: Build loop started, close out pending work first
        CLOSING_OUT -> VERIFYING: Close-out complete, verify system health
        VERIFYING -> FILLING: Verification passed, fill sprint backlog
        FILLING -> CLASSIFYING: Backlog filled, classify tickets
        CLASSIFYING -> BUILDING: Tickets classified, dispatch builds
        BUILDING -> COMPLETE: All builds dispatched
        COMPLETE -> IDLE: Cycle finished, ready for next
        Any -> FAILED: Unrecoverable error or circuit breaker tripped
    """

    IDLE = "idle"
    CLOSING_OUT = "closing_out"
    VERIFYING = "verifying"
    FILLING = "filling"
    CLASSIFYING = "classifying"
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"


class ModelBuildLoopEvent(BaseModel):
    """Event consumed by the build loop reducer to drive FSM transitions.

    Each event represents the outcome of a phase's work (success or failure),
    causing the reducer to compute the next state and emit intents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Cycle correlation ID for deduplication."
    )
    source_phase: EnumBuildLoopPhase = Field(
        ..., description="Phase that produced this event."
    )
    success: bool = Field(..., description="Whether the phase completed successfully.")
    timestamp: datetime = Field(..., description="When the event was produced.")
    error_message: str | None = Field(
        default=None, description="Error details if success=False."
    )
    tickets_filled: int = Field(
        default=0, ge=0, description="Tickets added during FILLING phase."
    )
    tickets_classified: int = Field(
        default=0, ge=0, description="Tickets classified during CLASSIFYING phase."
    )
    tickets_dispatched: int = Field(
        default=0, ge=0, description="Tickets dispatched during BUILDING phase."
    )


__all__: list[str] = ["EnumBuildLoopPhase", "ModelBuildLoopEvent"]
