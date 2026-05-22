"""Resolved context artifact input for context-pack assembly."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)
from pydantic import BaseModel, ConfigDict, Field


class ModelContextPackArtifact(BaseModel):
    """A pre-resolved artifact eligible to become a context chunk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor
    content: str = Field(min_length=1)
    token_estimate: int = Field(ge=0)
    provenance: EnumContextPackProvenance
    source_artifact_hash: str = Field(min_length=1)
    source_ticket_id: str | None = None
    source_contract_hash: str = Field(min_length=1)
    source_run_id: str | None = None
    source_priority: int = Field(default=100, ge=0)


__all__ = ["ModelContextPackArtifact"]
