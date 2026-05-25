# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for the swarm subtask state reducer."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    ModelSwarmRunState,
)


class EnumDelegationEventType(StrEnum):
    DELEGATION_EXECUTE = "delegation_execute"
    DELEGATION_CALL_COMPLETED = "delegation_call_completed"
    DELEGATION_ESCALATION_TRIGGERED = "delegation_escalation_triggered"
    DELEGATION_ALL_TIERS_FAILED = "delegation_all_tiers_failed"
    SWARM_FANOUT_COMPLETED = "swarm_fanout_completed"


class ModelDelegationEvent(BaseModel):
    """Normalized delegation event payload consumed by the reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: EnumDelegationEventType
    run_id: str
    subtask_id: str
    correlation_id: str
    endpoint_id: str = ""
    model_id: str = ""
    emitted_at: str = ""
    failure_class: str = ""
    source_topic: str = ""
    source_partition: int = 0
    source_offset: int = 0


class ModelSwarmSubtaskReducerInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelDelegationEvent
    current_state: ModelSwarmRunState | None = None


__all__: list[str] = [
    "EnumDelegationEventType",
    "ModelDelegationEvent",
    "ModelSwarmSubtaskReducerInput",
]
