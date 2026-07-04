from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.parity.model_env_parity import ModelEnvParityComputeResult


class ModelEnvParityLaneCollection(BaseModel):
    """Collection provenance for one runtime lane (never raw env values)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(..., min_length=1)
    compose_project: str = Field(default="")
    container_name: str = Field(default="")
    container_id: str = Field(default="")
    env_var_count: int = Field(
        default=0,
        ge=0,
        description="Number of env vars collected from the lane runtime container.",
    )
    collected: bool = Field(
        description="True when a live snapshot was captured for this lane."
    )
    optional: bool = Field(
        default=False,
        description=(
            "Contract-declared optional lane (e.g. the ephemeral dev lane); "
            "absence of an optional lane is not a parity gap."
        ),
    )
    collected_at: datetime | None = Field(
        default=None, description="UTC timestamp of the live snapshot."
    )
    detail: str = Field(default="")


class ModelEnvParityCollectResult(BaseModel):
    """Typed receipt: live collection provenance + parity verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: passed | gaps_detected | error")
    parity_ok: bool = Field(description="True when no parity gaps were detected.")
    scope: str = Field(..., min_length=1)
    collection_source: str = Field(
        default="",
        description=(
            "How the snapshots were obtained; 'live-ssh-docker-inspect' for a "
            "real collection run. Empty only on fail-fast errors."
        ),
    )
    ssh_target: str = Field(default="")
    collected_at: datetime | None = Field(
        default=None, description="UTC timestamp of the collection batch."
    )
    lane_collections: list[ModelEnvParityLaneCollection] = Field(default_factory=list)
    parity: ModelEnvParityComputeResult | None = Field(
        default=None,
        description="Parity verdict computed over the live snapshots.",
    )
    correlation_id: UUID | None = Field(default=None)
    error: str | None = Field(default=None)


__all__ = ["ModelEnvParityCollectResult", "ModelEnvParityLaneCollection"]
