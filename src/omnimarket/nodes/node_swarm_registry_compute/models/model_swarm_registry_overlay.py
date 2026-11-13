"""Overlay document model for the swarm endpoint registry (OMN-24053).

The document at ``<root>/node_swarm_registry_compute/overlay.yaml`` that
``HandlerSwarmRegistry`` reads; this model is what the overlays repository's CI
loads it through.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.nodes.node_swarm_registry_compute.models.model_registry_endpoint import (
    ModelRegistryEndpoint,
)


class ModelSwarmRegistryOverlay(BaseModel):
    """The swarm endpoint registry overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_schema_version: str
    endpoints: tuple[ModelRegistryEndpoint, ...]

    @field_validator("endpoints", mode="before")
    @classmethod
    def coerce_list(cls, v: object) -> object:
        if isinstance(v, list):
            return tuple(v)
        return v


__all__ = ["ModelSwarmRegistryOverlay"]
