from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ModelLaneCollectionLane(BaseModel):
    """One collectable runtime lane declared by the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    compose_project: str = Field(
        ...,
        min_length=1,
        description="docker compose project label identifying the lane.",
    )
    optional: bool = Field(
        default=False,
        description=(
            "True for lanes that are legitimately down most of the time "
            "(e.g. the ephemeral dev lane); their absence is recorded in "
            "provenance but not treated as a lane_missing parity gap."
        ),
    )

    @field_validator("compose_project")
    @classmethod
    def _validate_compose_project(cls, value: str) -> str:
        if not _SAFE_TOKEN.match(value):
            raise ValueError(f"unsafe compose_project token: {value!r}")
        return value


class ModelLaneCollectionConfig(BaseModel):
    """Contract-declared live collection topology for the lane host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_target_env_var: str = Field(
        ...,
        min_length=1,
        description=(
            "Env var consulted for the ssh destination when the request does "
            "not carry one."
        ),
    )
    runtime_service_label: str = Field(
        ...,
        min_length=1,
        description=(
            "docker compose service label of the lane runtime container to "
            "snapshot (com.docker.compose.service)."
        ),
    )
    lanes: list[ModelLaneCollectionLane] = Field(..., min_length=1)

    @field_validator("runtime_service_label")
    @classmethod
    def _validate_service_label(cls, value: str) -> str:
        if not _SAFE_TOKEN.match(value):
            raise ValueError(f"unsafe runtime_service_label token: {value!r}")
        return value

    @field_validator("lanes")
    @classmethod
    def _validate_unique_lanes(
        cls, value: list[ModelLaneCollectionLane]
    ) -> list[ModelLaneCollectionLane]:
        names = [lane.name for lane in value]
        if len(names) != len(set(names)):
            raise ValueError("lane_collection lanes must have unique names")
        return value


__all__ = ["ModelLaneCollectionConfig", "ModelLaneCollectionLane"]
