"""Request model for deterministic context-pack assembly."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_pack_artifact import (
    ModelContextPackArtifact,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_profile import (
    ModelContextProfile,
)


class ModelContextPackBuilderRequest(BaseModel):
    """All context-pack inputs, already resolved by upstream effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_hash: str = Field(min_length=1)
    generated_at: str = Field(
        ...,
        min_length=1,
        description="Timezone-aware ISO8601 UTC timestamp supplied by caller.",
    )
    profile: ModelContextProfile
    artifacts: tuple[ModelContextPackArtifact, ...] = Field(default_factory=tuple)


__all__ = ["ModelContextPackBuilderRequest"]
