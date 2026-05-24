"""Input model for swarm endpoint selection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.nodes.node_swarm_registry_compute.models.enums import (
    EnumEndpointStatus,
    EnumModelStatus,
)


class ModelSubtask(BaseModel):
    """Local definition of a subtask to be assigned an endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    description: str
    category: str
    estimated_tokens: int = 0
    model_affinity: str = ""


class ModelEndpointHealth(BaseModel):
    """Health status of a single endpoint, produced by the health effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_id: str
    endpoint_status: EnumEndpointStatus
    model_status: EnumModelStatus


class ModelSwarmEndpointSelectionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtasks: tuple[ModelSubtask, ...]
    endpoint_health: dict[str, ModelEndpointHealth]
    registry_hash: str

    @field_validator("subtasks", mode="before")
    @classmethod
    def coerce_subtasks(cls, v: object) -> object:
        if isinstance(v, list):
            return tuple(v)
        return v


__all__ = [
    "ModelEndpointHealth",
    "ModelSubtask",
    "ModelSwarmEndpointSelectionRequest",
]
