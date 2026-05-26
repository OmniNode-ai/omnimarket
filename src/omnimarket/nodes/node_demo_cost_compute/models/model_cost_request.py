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

from omnimarket.events.demo import (
    ModelDemoCostEntry,
    ModelDemoCostResult,
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


class ModelDemoCostRequest(BaseModel):
    """Input to the demo cost compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inference_results: list[ModelDemoInferenceResult] = Field(min_length=1)
    pricing_table: dict[str, ModelDemoModelPricing] = Field(
        description="Keyed by model_id; models missing from table are assigned zero cost"
    )


__all__ = [
    "ModelDemoCostEntry",
    "ModelDemoCostRequest",
    "ModelDemoCostResult",
    "ModelDemoInferenceResult",
    "ModelDemoModelPricing",
]
