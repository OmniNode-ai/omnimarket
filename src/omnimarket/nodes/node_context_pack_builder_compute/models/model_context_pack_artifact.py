"""Resolved context artifact input for context-pack assembly."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelContextPackArtifact(BaseModel):
    """A pre-resolved artifact eligible to become a context chunk.

    Provenance fields (`source_file`, `heading_path`, `char_count`,
    `reason_selected`) are optional — existing callers that omit them remain
    valid.  Section-parsed guidance artifacts populate all four.
    """

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

    # Section-parser provenance — populated when content was split from a
    # guidance file by GuidanceSectionParser; None for all other artifact types.
    source_file: str | None = None
    heading_path: tuple[str, ...] | None = None
    char_count: int | None = Field(default=None, ge=0)
    reason_selected: str | None = None

    @field_validator("heading_path", mode="before")
    @classmethod
    def _coerce_heading_path(cls, v: object) -> tuple[str, ...] | None:
        """Accept list[str] from callers and normalise to tuple for frozen storage."""
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return tuple(str(item) for item in v)
        raise ValueError(f"heading_path must be a list or tuple, got {type(v)!r}")


__all__ = ["ModelContextPackArtifact"]
