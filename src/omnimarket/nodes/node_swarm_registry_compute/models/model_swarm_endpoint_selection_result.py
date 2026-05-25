"""Output model for swarm endpoint selection."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class ModelEndpointSelectionEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    assigned_endpoint_id: str
    reason: str


class ModelSwarmEndpointSelectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    assignments: dict[str, str]
    unroutable_subtasks: tuple[str, ...] = ()
    selection_evidence: tuple[ModelEndpointSelectionEvidence, ...] = ()
    run_id: str = ""

    @field_validator("unroutable_subtasks", mode="before")
    @classmethod
    def coerce_unroutable(cls, v: object) -> object:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("selection_evidence", mode="before")
    @classmethod
    def coerce_evidence(cls, v: object) -> object:
        if isinstance(v, list):
            return tuple(v)
        return v


__all__ = [
    "ModelEndpointSelectionEvidence",
    "ModelSwarmEndpointSelectionResult",
]
