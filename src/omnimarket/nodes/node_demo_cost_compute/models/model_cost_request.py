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

from omnimarket.events.demo_pipeline import (
    ModelDemoCostEntry as ModelDemoCostEntry,
)
from omnimarket.events.demo_pipeline import (
    ModelDemoCostResult as ModelDemoCostResult,
)
from omnimarket.events.demo_pipeline import (
    ModelDemoInferenceResult as ModelDemoInferenceResult,
)
from omnimarket.events.demo_pipeline import (
    ModelDemoModelPricing as ModelDemoModelPricing,
)

__all__ = [
    "ModelDemoCostEntry",
    "ModelDemoCostRequest",
    "ModelDemoCostResult",
    "ModelDemoInferenceResult",
    "ModelDemoModelPricing",
]


class ModelDemoCostRequest(BaseModel):
    """Input to the demo cost compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inference_results: list[ModelDemoInferenceResult] = Field(min_length=1)
    pricing_table: dict[str, ModelDemoModelPricing] = Field(
        description="Keyed by model_id; models missing from table are assigned zero cost"
    )
