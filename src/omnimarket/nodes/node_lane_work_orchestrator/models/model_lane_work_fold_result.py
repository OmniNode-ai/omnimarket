# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one call of the fold did (OMN-26721)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_output import (
    ModelLaneWorkOutput,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_step_completed import (
    ModelLaneWorkStepCompleted,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_transition_record import (
    ModelLaneWorkTransitionRecord,
)


class ModelLaneWorkFoldResult(BaseModel):
    """``published`` lists what this call put on the bus, in order; a duplicate publishes
    nothing new for the receipt, only the reply for the new correlation id and any output
    the first attempt did not reach."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[
        "transition", "refused", "duplicate", "status", "acknowledged", "dropped"
    ]
    record: ModelLaneWorkTransitionRecord | None = None
    reply: ModelLaneWorkStepCompleted | None = None
    published: tuple[ModelLaneWorkOutput, ...] = ()
    refusal: str | None = None


__all__: list[str] = ["ModelLaneWorkFoldResult"]
