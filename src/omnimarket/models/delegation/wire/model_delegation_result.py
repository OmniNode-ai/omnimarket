# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation result wire DTO."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    pass


class ModelDelegationResult(BaseModel):
    """Delegation outcome: content, quality status, model info, and metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(
        ...,
        description="Tracks this result back to the original request.",
    )
    task_type: str = Field(
        ..., description="The task classification from the original request."
    )
    model_used: str = Field(
        ...,
        description="Name of the LLM model that produced the response.",
    )
    endpoint_url: str = Field(..., description="URL of the LLM endpoint used.")
    content: str = Field(..., description="The LLM-generated response content.")
    quality_passed: bool = Field(
        ...,
        description="Whether the quality gate accepted the response.",
    )
    quality_score: float = Field(..., description="Quality score from 0.0 to 1.0.")
    latency_ms: int = Field(..., description="End-to-end latency in milliseconds.")
    prompt_tokens: int = Field(default=0, description="Number of tokens in the prompt.")
    completion_tokens: int = Field(
        default=0, description="Number of tokens in the completion."
    )
    total_tokens: int = Field(
        default=0, description="Total tokens used (prompt + completion)."
    )
    fallback_to_claude: bool = Field(
        ...,
        description="Whether fallback to Claude was triggered.",
    )
    failure_reason: str = Field(
        default="",
        description="Reason for failure, empty string if successful.",
    )
    tokens_to_compliance: int = Field(
        default=0,
        ge=0,
        description="Total tokens across all compliance attempts.",
    )
    compliance_attempts: int = Field(
        default=1,
        ge=1,
        description="Number of LLM invocations to reach compliance.",
    )

    # --- Escalation fields (OMN-12254) ---
    escalation_count: int = Field(
        default=0,
        description="Number of escalation attempts that occurred.",
    )
    escalation_history: tuple[dict[str, object], ...] = Field(
        default=(),
        description=(
            "Serialized escalation attempt records. Each entry is a dict "
            "representation of ModelDelegationEscalationAttempt."
        ),
    )
    terminal_failure_reason: str | None = Field(
        default=None,
        description=(
            "Populated on FAILED terminal events. One of: "
            "fallback_not_recommended, max_escalation_attempts_reached, "
            "no_higher_tier_available, current_tier_unknown."
        ),
    )
    routing_tiers_hash: str | None = Field(
        default=None,
        description="SHA-256 of serialized routing_tiers.yaml at execution time.",
    )
    escalation_config_hash: str | None = Field(
        default=None,
        description="SHA-256 of the escalation section of contract.yaml at execution time.",
    )
    final_attempt_cost: float = Field(
        default=0.0,
        description="Cost of the final (successful or last) attempt.",
    )
    cumulative_attempt_cost: float = Field(
        default=0.0,
        description="Total cost across all escalation attempts.",
    )
    cumulative_input_tokens: int = Field(
        default=0,
        description="Total input tokens across all attempts.",
    )
    cumulative_output_tokens: int = Field(
        default=0,
        description="Total output tokens across all attempts.",
    )
    attempts_count: int = Field(
        default=1,
        description="Total attempts including the initial one.",
    )


__all__: list[str] = ["ModelDelegationResult"]
