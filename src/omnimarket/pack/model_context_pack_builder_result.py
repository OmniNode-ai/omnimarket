"""Result model for deterministic context-pack assembly."""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.models.pack.model_context_pack import ModelContextPack
from pydantic import BaseModel, ConfigDict, Field


class EnumContextPackBuilderStatus(StrEnum):
    """Lifecycle state for context-pack assembly."""

    OK = "ok"
    FAILED = "failed"


class ModelContextPackDroppedDuplicate(BaseModel):
    """An artifact removed because its normalised content equals a kept artifact's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor
    source_artifact_hash: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kept_factor: EnumContextFactor
    kept_source_artifact_hash: str = Field(min_length=1)


class ModelContextPackBudgetExclusion(BaseModel):
    """An optional artifact left out because it did not fit the token budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor
    source_artifact_hash: str = Field(min_length=1)
    token_estimate: int = Field(ge=0)


class ModelContextPackBuilderResult(BaseModel):
    """Context-pack build result with typed failure details."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextPackBuilderStatus
    context_pack: ModelContextPack | None = None
    pack_hash: str | None = None
    failure_class: EnumContextPackFailure | None = None
    errors: tuple[str, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    dropped_duplicates: tuple[ModelContextPackDroppedDuplicate, ...] = ()
    excluded_by_budget: tuple[ModelContextPackBudgetExclusion, ...] = ()


__all__ = [
    "EnumContextPackBuilderStatus",
    "ModelContextPackBudgetExclusion",
    "ModelContextPackBuilderResult",
    "ModelContextPackDroppedDuplicate",
]
