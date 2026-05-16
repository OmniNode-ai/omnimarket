# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-facing delegation response model.

Domain-level (provider, model, response text, quality gate, metrics) rather than
transport-level — it is not the Codex adapter response shape.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

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
    latency_ms: int = Field(default=0, ge=0)


class ModelDelegateSkillResponse(BaseModel):
    """Typed delegation result returned to the requesting adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["completed", "failed", "timeout"] = Field(...)
    correlation_id: UUID = Field(...)
    task_type: str = Field(...)
    provider: str = Field(default="")
    model_name: str = Field(default="")
    response: str = Field(default="")
    quality_gate_passed: bool = Field(default=False)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    quality_gates_failed: list[str] = Field(default_factory=list)
    metrics: ModelDelegateSkillResponseMetrics = Field(
        default_factory=ModelDelegateSkillResponseMetrics,
    )
    error_message: str = Field(default="")
