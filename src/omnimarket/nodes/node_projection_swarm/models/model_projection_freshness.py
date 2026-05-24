"""ModelProjectionFreshness — freshness tracking for the swarm projection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_swarm.models.enums import EnumFreshnessState


class ModelProjectionFreshness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    freshness_state: EnumFreshnessState = Field(
        ..., description="Current freshness state."
    )
    degraded_reason: str = Field(
        default="", description="Populated when state is degraded or stale."
    )
    projection_cursor: str = Field(
        default="", description="Logical cursor for replay position."
    )
    source_event_id: str = Field(
        default="", description="ID of the event that triggered this projection."
    )
    source_topic: str = Field(
        default="", description="Topic the source event arrived on."
    )
    source_partition: int = Field(default=0, ge=0)
    source_offset: int = Field(default=0, ge=0)
    reducer_version: str = Field(default="1.0.0")
    observed_at: str = Field(
        ..., description="ISO 8601 timestamp when this freshness record was computed."
    )


__all__: list[str] = ["ModelProjectionFreshness"]
