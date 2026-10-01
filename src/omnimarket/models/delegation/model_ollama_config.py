# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Ollama default-model block of bifrost_delegation.yaml.

Deliberately outside ``models/delegation/wire``: the block is routing
configuration read by ``load_ollama_config``, lifted off before the wire model
validates, so the shape every released consumer accepts is unchanged.
"""

from __future__ import annotations

from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelOllamaModelTier(BaseModel):
    """One default Ollama model and the smallest Mac memory that selects it."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    min_memory_gb: int = Field(
        ..., ge=0, description="Smallest memory (GiB) selecting this model."
    )
    model: str = Field(..., min_length=1, description="Ollama model tag to pull.")
    download_gb: float = Field(
        ..., gt=0, description="Download size in GB, checked against free disk."
    )


class ModelOllamaConfig(BaseModel):
    """Ollama port, chat path and per-memory default model. No host."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    port: int = Field(..., ge=1, le=65535)
    chat_path: str = Field(..., pattern=r"^/")
    models: tuple[ModelOllamaModelTier, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _models_ordered_and_floored(self) -> ModelOllamaConfig:
        floors = [tier.min_memory_gb for tier in self.models]
        if any(a <= b for a, b in pairwise(floors)):
            msg = f"ollama.models must be ordered by min_memory_gb strictly descending, got {floors}"
            raise ValueError(msg)
        if floors[-1] != 0:
            msg = f"the last ollama.models entry must have min_memory_gb 0, got {floors[-1]}"
            raise ValueError(msg)
        return self


__all__: list[str] = ["ModelOllamaConfig", "ModelOllamaModelTier"]
