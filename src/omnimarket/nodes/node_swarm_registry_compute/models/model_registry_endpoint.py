"""Registry endpoint model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.nodes.node_swarm_registry_compute.models.enums import (
    EnumSwarmCapability,
)


class ModelRegistryEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    base_url: str
    model_id: str
    provider: str
    capabilities: tuple[EnumSwarmCapability, ...]
    context_window: int | None = None
    context_window_source: str = "unknown"
    cost_basis: str = "local"
    health_check_path: str = "/health"
    declared_by: str = ""
    declared_at: str = ""
    endpoint_ref: str = ""

    @field_validator("capabilities", mode="before")
    @classmethod
    def coerce_list(cls, v: object) -> object:
        if isinstance(v, list):
            return tuple(v)
        return v


__all__ = ["ModelRegistryEndpoint"]
