# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelABComparisonResult -- terminal output for node_delegation_ab_runner."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_ab_runner.models.model_delegation_path_result import (
    ModelDelegationPathResult,
)


class ModelABComparisonResult(BaseModel):
    """Phase 4 comparison table: baseline vs delegated path results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Correlation ID from the request.")
    task_payload_hash: str = Field(
        ..., description="SHA-256 of the task_payload for reproducibility."
    )
    baseline: ModelDelegationPathResult = Field(
        ..., description="Frontier model baseline results."
    )
    delegated: ModelDelegationPathResult = Field(
        ..., description="Delegated path results."
    )

    token_savings: int = Field(
        default=0,
        description="baseline.total_tokens - delegated.total_tokens (negative = delegated used more).",
    )
    cost_savings_usd: float = Field(
        default=0.0,
        description="baseline.cost_usd - delegated.cost_usd (negative = delegated was more expensive).",
    )
    latency_delta_ms: int = Field(
        default=0,
        description="delegated.latency_ms - baseline.latency_ms (negative = delegated was faster).",
    )
    delegated_quality_passed: bool = Field(
        default=True,
        description="True if the delegated path passed the quality gate.",
    )
    winner: str = Field(
        default="",
        description="'delegated' if delegated passed quality gate and saved cost; 'baseline' otherwise.",
    )
    pricing_manifest_hash: str = Field(
        default="", description="Pricing manifest hash used for cost calc."
    )

    @classmethod
    def compute(
        cls,
        correlation_id: str,
        task_payload_hash: str,
        baseline: ModelDelegationPathResult,
        delegated: ModelDelegationPathResult,
        pricing_manifest_hash: str = "",
    ) -> ModelABComparisonResult:
        token_savings = baseline.total_tokens - delegated.total_tokens
        cost_savings = round(baseline.cost_usd - delegated.cost_usd, 8)
        latency_delta = delegated.latency_ms - baseline.latency_ms
        winner = (
            "delegated"
            if (delegated.quality_passed and not delegated.error and cost_savings >= 0)
            else "baseline"
        )
        return cls(
            correlation_id=correlation_id,
            task_payload_hash=task_payload_hash,
            baseline=baseline,
            delegated=delegated,
            token_savings=token_savings,
            cost_savings_usd=cost_savings,
            latency_delta_ms=latency_delta,
            delegated_quality_passed=delegated.quality_passed,
            winner=winner,
            pricing_manifest_hash=pricing_manifest_hash,
        )


__all__ = ["ModelABComparisonResult"]
