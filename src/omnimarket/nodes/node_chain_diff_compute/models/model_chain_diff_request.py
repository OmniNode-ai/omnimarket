"""Request model for deterministic golden-chain comparison."""

from __future__ import annotations

from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry
from pydantic import BaseModel, ConfigDict, Field


class ModelChainDiffRequest(BaseModel):
    """Expected and observed event-chain entries for pure diffing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: tuple[ModelGoldenChainEntry, ...] = Field(default_factory=tuple)
    observed: tuple[ModelGoldenChainEntry, ...] = Field(default_factory=tuple)


__all__ = ["ModelChainDiffRequest"]
