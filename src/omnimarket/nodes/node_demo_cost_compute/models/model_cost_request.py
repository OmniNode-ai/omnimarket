# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_demo_cost_compute [OMN-12235].

Contains:
- ModelDemoModelPricing: per-model pricing config (cost per 1k tokens)
- ModelDemoCostEntry: computed cost for a single model result
- ModelDemoCostRequest: input to the cost compute node
- ModelDemoCostResult: output with per-model costs and cheapest model
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
    ModelDemoInferenceResult,
)


class ModelDemoModelPricing(BaseModel):
    """Per-model pricing config in USD per 1k tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_cost_per_1k: float = Field(
        ge=0.0, description="Cost per 1k prompt tokens in USD"
    )
    completion_cost_per_1k: float = Field(
        ge=0.0, description="Cost per 1k completion tokens in USD"
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


class ModelDemoCostRequest(BaseModel):
    """Input to the demo cost compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inference_results: list[ModelDemoInferenceResult] = Field(min_length=1)
    pricing_table: dict[str, ModelDemoModelPricing] = Field(
        description="Keyed by model_id; models missing from table are assigned zero cost"
    )


class ModelDemoCostResult(BaseModel):
    """Output: per-model cost entries and the cheapest model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    costs: list[ModelDemoCostEntry]
    cheapest_model_id: str | None = Field(
        default=None,
        description="model_id with the lowest total_cost_usd; null when costs list is empty",
    )
