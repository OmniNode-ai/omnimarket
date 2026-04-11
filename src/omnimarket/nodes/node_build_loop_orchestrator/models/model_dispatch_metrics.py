# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Aggregate metrics for a single dispatch run.

Computed from all ModelDispatchTrace records after a run completes.
Written to .onex_state/dispatch-metrics/{correlation_id}.json and
optionally emitted as onex.evt.omnimarket.delegation-metrics.v1.

Related:
    - OMN-7858: Add dispatch metrics summary
    - OMN-7855: Add dispatch tracing to .onex_state/
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDispatchMetrics(BaseModel):
    """Aggregate metrics computed from all dispatch traces for a single run.

    Answers the key operational question: how many iterations does
    Qwen3-Coder need with GLM review, and what does it cost?
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Root correlation ID for the run.")
    total_tickets: int = Field(..., ge=0, description="Total tickets attempted.")
    accepted_count: int = Field(
        ..., ge=0, description="Tickets with at least one accepted attempt."
    )
    rejected_count: int = Field(
        ..., ge=0, description="Tickets where all attempts were rejected."
    )
    total_generation_attempts: int = Field(
        ..., ge=0, description="Sum of all generation attempts across all tickets."
    )
    total_review_iterations: int = Field(
        ...,
        ge=0,
        description="Sum of all review calls (attempts that reached the review stage).",
    )
    avg_attempts_per_ticket: float = Field(
        ...,
        ge=0.0,
        description="Average generation attempts per ticket (0.0 if no tickets).",
    )
    total_prompt_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Sum of prompt tokens across all generation attempts. "
            "None when no trace had token data (tokenization unavailable)."
        ),
    )
    total_completion_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Sum of completion tokens across all generation attempts. "
            "None when no trace had token data (tokenization unavailable)."
        ),
    )
    total_review_tokens: int = Field(
        ..., ge=0, description="Sum of review tokens across all review calls."
    )
    total_wall_clock_ms: int = Field(
        ..., ge=0, description="Sum of wall-clock time across all attempts in ms."
    )
    coder_model: str = Field(..., description="Model ID used for code generation.")
    reviewer_model: str | None = Field(
        default=None,
        description="Model ID used for review (None if reviewer was unavailable).",
    )
    quality_gate_failure_rate: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of attempts that failed the structural gate before reaching review. "
            "0.0 if no attempts."
        ),
    )
    review_rejection_rate: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of gate-passing attempts that were rejected by the reviewer. "
            "0.0 if no gate-passing attempts or no reviewer."
        ),
    )


__all__: list[str] = ["ModelDispatchMetrics"]
