# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-facing delegate-skill response model."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual
from pydantic import BaseModel, ConfigDict, Field


class ModelDelegateSkillResponseMetrics(BaseModel):
    """Cost and latency metrics for a delegation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    tokens_to_compliance: int = Field(default=0, ge=0)
    compliance_attempts: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    cost_savings_usd: float = Field(default=0.0, ge=0.0)
    frontier_costs_usd: dict[str, float] = Field(default_factory=dict)
    premium_counterfactual: ModelPremiumCounterfactual | None = Field(
        default=None,
        description=(
            "Pinned premium counterfactual {model, price, as_of, tokens, cost} "
            "(OMN-13355). cost_savings_usd = counterfactual_cost_usd - cost_usd."
        ),
    )
    latency_ms: int = Field(default=0, ge=0)


class ModelDelegateSkillResponse(BaseModel):
    """Typed delegation result returned to requesting adapters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["completed", "failed", "timeout"] = Field(...)
    correlation_id: UUID = Field(...)
    task_type: str = Field(...)
    provider: str = Field(default="")
    model_name: str = Field(default="")
    model_cloud_baseline: str = Field(default="")
    pricing_manifest_version: int = Field(default=0, ge=0)
    prompt_text: str = Field(default="")
    response: str = Field(default="")
    quality_gate_passed: bool = Field(default=False)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    quality_gates_failed: list[str] = Field(default_factory=list)
    metrics: ModelDelegateSkillResponseMetrics = Field(
        default_factory=ModelDelegateSkillResponseMetrics,
    )
    error_message: str = Field(default="")


__all__ = [
    "ModelDelegateSkillResponse",
    "ModelDelegateSkillResponseMetrics",
]
