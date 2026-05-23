"""Result models for deterministic ticket-contract test generation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumTestGenerationStatus(StrEnum):
    """Lifecycle state for generated test artifacts."""

    OK = "ok"
    INSUFFICIENT_CONTRACT = "insufficient_contract"
    FAILED = "failed"


class ModelGeneratedTestFile(BaseModel):
    """A generated test file artifact that an effect node may persist later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_sha256: str = Field(min_length=64, max_length=64)
    pytest_node_ids: tuple[str, ...] = Field(default_factory=tuple)
    source_refs: tuple[str, ...] = Field(default_factory=tuple)


class ModelTestGenerationResult(BaseModel):
    """Deterministic output from node_test_generator_compute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumTestGenerationStatus
    ticket_id: str
    contract_hash: str
    contract_fingerprint: str | None
    generator_version: str
    template_hash: str
    generation_profile_hash: str
    generated_files: tuple[ModelGeneratedTestFile, ...] = Field(default_factory=tuple)
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    failure_class: str | None = None
    parser_error: str | None = None


__all__ = [
    "EnumTestGenerationStatus",
    "ModelGeneratedTestFile",
    "ModelTestGenerationResult",
]
