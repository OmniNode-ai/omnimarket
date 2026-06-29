"""Result model for deterministic context-pack assembly."""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.models.pack.model_context_pack import ModelContextPack
from pydantic import BaseModel, ConfigDict, Field


class EnumContextPackBuilderStatus(StrEnum):
    """Lifecycle state for context-pack assembly."""

    OK = "ok"
    FAILED = "failed"


class ModelContextPackBuilderResult(BaseModel):
    """Context-pack build result with typed failure details."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextPackBuilderStatus
    context_pack: ModelContextPack | None = None
    pack_hash: str | None = None
    failure_class: EnumContextPackFailure | None = None
    errors: tuple[str, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)


__all__ = ["EnumContextPackBuilderStatus", "ModelContextPackBuilderResult"]
