# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Baseline comparison intent emitted by the delegation orchestrator.

Carries the estimated Claude cost (baseline) and the actual local LLM
cost (candidate, near-zero for self-hosted) for the savings computation
pipeline.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelBaselineIntent(BaseModel):
    """Intent to feed savings data into node_baseline_comparison_compute.

    Phase 1 savings estimation is comparative and approximate:
    baseline_cost_usd is modeled from prompt length x Claude pricing,
    candidate_cost_usd is near-zero for self-hosted local models.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: str = Field(default="baseline_comparison")
    correlation_id: UUID = Field(..., description="Delegation correlation ID.")
    task_type: str = Field(..., description="Task classification.")
    baseline_cost_usd: float = Field(
        ..., description="Estimated Claude cost for this task."
    )
    candidate_cost_usd: float = Field(
        default=0.0, description="Actual local LLM cost (near-zero for self-hosted)."
    )
    prompt_tokens: int = Field(default=0, description="Prompt token count.")
    completion_tokens: int = Field(default=0, description="Completion token count.")
    total_tokens: int = Field(default=0, description="Total token count.")


__all__: list[str] = ["ModelBaselineIntent"]
