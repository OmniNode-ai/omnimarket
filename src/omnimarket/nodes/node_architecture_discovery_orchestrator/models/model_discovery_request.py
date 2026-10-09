"""Run arguments, preserving the former workflow's caller spellings."""

from __future__ import annotations

from pathlib import PurePath
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class ModelDiscoveryProfile(BaseModel):
    """A caller-supplied suitability profile, never an implicit person."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    surfaces: tuple[str, ...]
    excluded_domains: tuple[str, ...]
    planning_loop_eligible: StrictBool


class ModelDiscoveryOverlay(BaseModel):
    """Operator-private facts. The caller reads them from its overlay; this package holds none."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    substrate_probe_url: str = Field(min_length=1)
    registry_dir: str = Field(min_length=1)


class ModelDiscoveryRequest(BaseModel):
    """Explicit run date; state is optional and never gates a full scan."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    date: str = Field(min_length=1)
    write_state: StrictBool = Field(default=False, alias="writeState")
    profiles: tuple[ModelDiscoveryProfile, ...] = ()
    fences: tuple[str, ...] = ()
    overlay: ModelDiscoveryOverlay
    workspace_path: str
    correlation_id: UUID = Field(default_factory=uuid4)

    @field_validator("workspace_path")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if not PurePath(value).is_absolute():
            raise ValueError(
                "workspace_path must name an absolute runtime-visible worktree"
            )
        return value
