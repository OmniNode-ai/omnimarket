# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared generation models for test and golden-chain pipeline nodes."""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract
from pydantic import BaseModel, ConfigDict, Field


class ModelTestGenerationRequest(BaseModel):
    """Inputs for pure generated-test artifact creation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: ModelTicketContract
    generator_version: str = Field(default="1.0.0", min_length=1)
    generation_profile_hash: str = Field(default="profile_default", min_length=1)


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


class ModelGoldenChainGenerationRequest(BaseModel):
    """Inputs for deriving an expected chain from contract truth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: ModelTicketContract
    generated_test_hash: str | None = None
    generator_version: str = Field(default="1.0.0", min_length=1)
    template_hash: str | None = None


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
    "EnumTestGenerationStatus",
    "ModelDeferredChainWarning",
    "ModelGeneratedTestFile",
    "ModelGoldenChainGenerationRequest",
    "ModelGoldenChainGenerationResult",
    "ModelTestGenerationRequest",
    "ModelTestGenerationResult",
]
