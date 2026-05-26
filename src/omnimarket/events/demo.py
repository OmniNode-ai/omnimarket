# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared demo workflow event models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDemoInferenceResult(BaseModel):
    """Per-model inference result from a fan-out run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0.0, description="Wall-clock latency in milliseconds")
    output_text: str = Field(
        default="", description="Generated text (may be empty for stub)"
    )
    error: str | None = Field(
        default=None, description="Error message if inference failed"
    )


class ModelDemoCostEntry(BaseModel):
    """Computed cost breakdown for a single model inference result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    prompt_cost_usd: float = Field(ge=0.0)
    completion_cost_usd: float = Field(ge=0.0)
    total_cost_usd: float = Field(ge=0.0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class ModelDemoCostResult(BaseModel):
    """Output: per-model cost entries and the cheapest model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    costs: list[ModelDemoCostEntry]
    cheapest_model_id: str | None = Field(
        default=None,
        description="model_id with the lowest total_cost_usd; null when costs list is empty",
    )


__all__ = [
    "ModelDemoCostEntry",
    "ModelDemoCostResult",
    "ModelDemoInferenceResult",
]
