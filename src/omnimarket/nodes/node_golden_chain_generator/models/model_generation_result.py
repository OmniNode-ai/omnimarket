# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for golden chain generation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_golden_chain_generator.models.model_golden_chain_entry import (
    ModelGoldenChainEntry,
)

__all__ = ["EnumGenerationStatus", "ModelGoldenChainGenerationResult"]


class EnumGenerationStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ModelGoldenChainGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumGenerationStatus
    expected_chain: tuple[ModelGoldenChainEntry, ...] = Field(default_factory=tuple)
    chain_hash: str = ""
    contract_hash: str = ""
    generator_version: str = ""
    template_hash: str = ""
    generation_profile_hash: str = ""
    generated_at: str = ""
    error: str | None = None
