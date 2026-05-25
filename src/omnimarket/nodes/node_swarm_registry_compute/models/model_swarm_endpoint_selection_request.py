"""Input model for swarm endpoint selection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

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
    depends_on: tuple[str, ...] = ()


class ModelEndpointHealth(BaseModel):
    """Health status of a single endpoint, produced by the health effect node.

    Accepts both the orchestrator's shape (``status`` str field) and the
    health-effect node's shape (``endpoint_status`` / ``model_status``
    enum fields).  When the orchestrator sends ``status`` without
    ``endpoint_status``, the model_validator promotes ``status`` into
    ``endpoint_status``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_id: str
    # Canonical typed fields — used by the selection logic.
    endpoint_status: EnumEndpointStatus = EnumEndpointStatus.unreachable
    model_status: EnumModelStatus = EnumModelStatus.unknown
    # Fields the orchestrator forwards from its internal state.
    status: str = ""
    latency_ms: int | None = None
    error: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _promote_status(cls, values: dict[str, object]) -> dict[str, object]:
        """If only ``status`` is supplied, copy it into ``endpoint_status``."""
        if (
            isinstance(values, dict)
            and "endpoint_status" not in values
            and "status" in values
        ):
            values["endpoint_status"] = values["status"]
        return values


class ModelSwarmEndpointSelectionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtasks: tuple[ModelSubtask, ...]
    endpoint_health: dict[str, ModelEndpointHealth]
    registry_hash: str = ""
    correlation_id: str = ""
    run_id: str = ""

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
