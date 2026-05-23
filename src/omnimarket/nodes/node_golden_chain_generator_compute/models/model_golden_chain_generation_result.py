"""Result model for deterministic golden-chain generation."""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry
from pydantic import BaseModel, ConfigDict, Field


class EnumGoldenChainGenerationStatus(StrEnum):
    """Lifecycle state for expected-chain generation."""

    OK = "ok"
    DEFERRED = "deferred"
    INSUFFICIENT_CONTRACT = "insufficient_contract"


class ModelDeferredChainWarning(BaseModel):
    """A deterministic warning for topology that cannot be proven statically."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class ModelGoldenChainGenerationResult(BaseModel):
    """Expected-chain artifact plus deterministic provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumGoldenChainGenerationStatus
    ticket_id: str
    contract_hash: str
    contract_fingerprint: str | None
    chain_hash: str
    generator_version: str
    template_hash: str | None = None
    generated_test_hash: str | None = None
    expected_chain: tuple[ModelGoldenChainEntry, ...] = Field(default_factory=tuple)
    deferred_warnings: tuple[ModelDeferredChainWarning, ...] = Field(
        default_factory=tuple
    )


__all__ = [
    "EnumGoldenChainGenerationStatus",
    "ModelDeferredChainWarning",
    "ModelGoldenChainGenerationResult",
]
