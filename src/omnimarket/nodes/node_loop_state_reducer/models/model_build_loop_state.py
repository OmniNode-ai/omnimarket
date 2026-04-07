# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build loop state model — the canonical FSM state for the autonomous build loop.

Migrated from omnibase_infra [OMN-7577].

Related:
    - OMN-7311: ModelBuildLoopState foundation models
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_loop_state_reducer.models.model_build_loop_event import (
    EnumBuildLoopPhase,
)


class ModelBuildLoopState(BaseModel):
    """Frozen FSM state for the autonomous build loop.

    The reducer is the ONLY authority that produces new instances of this model.
    All phase transitions go through the reducer's delta function.

    Circuit breaker: after ``max_consecutive_failures`` consecutive failures,
    the reducer transitions to FAILED and emits a CIRCUIT_BREAK intent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Unique ID for this build loop cycle."
    )
    phase: EnumBuildLoopPhase = Field(
        default=EnumBuildLoopPhase.IDLE,
        description="Current FSM phase.",
    )
    cycle_number: int = Field(
        default=0, ge=0, description="Monotonically increasing cycle counter."
    )
    max_cycles: int = Field(
        default=1, ge=1, description="Maximum cycles before auto-stop."
    )
    consecutive_failures: int = Field(
        default=0, ge=0, description="Consecutive failure count for circuit breaker."
    )
    max_consecutive_failures: int = Field(
        default=3, ge=1, description="Circuit breaker threshold."
    )
    skip_closeout: bool = Field(
        default=False, description="Skip the CLOSING_OUT phase."
    )
    dry_run: bool = Field(
        default=False, description="If true, no side-effects are executed."
    )
    started_at: datetime | None = Field(
        default=None, description="Timestamp when the current cycle started."
    )
    last_phase_at: datetime | None = Field(
        default=None, description="Timestamp of the last phase transition."
    )
    error_message: str | None = Field(
        default=None, description="Error message if phase is FAILED."
    )
    tickets_filled: int = Field(
        default=0, ge=0, description="Number of tickets filled in FILLING phase."
    )
    tickets_classified: int = Field(
        default=0, ge=0, description="Number of tickets classified."
    )
    tickets_dispatched: int = Field(
        default=0, ge=0, description="Number of tickets dispatched for building."
    )


__all__: list[str] = ["ModelBuildLoopState"]
