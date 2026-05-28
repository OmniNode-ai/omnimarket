# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelDelegationPathResult -- per-path result for one A/B run leg."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationPathResult(BaseModel):
    """Measured outcome for one path (baseline or delegated) in the A/B run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(..., description="Path label matching the request config.")
    model_id: str = Field(..., description="Model that handled this path.")
    endpoint_url: str = Field(..., description="Endpoint called.")
    is_delegated: bool = Field(..., description="True for the delegated path.")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(
        default=0.0, ge=0.0, description="Calculated or estimated cost in USD."
    )
    latency_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(
        default=0, ge=0, description="Number of retries before success or failure."
    )
    quality_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Quality gate score (0-1). 0 = not evaluated.",
    )
    quality_passed: bool = Field(
        default=True,
        description="True if quality gate passed or was not evaluated.",
    )
    escalated: bool = Field(
        default=False,
        description="True if this delegated call escalated to the frontier model.",
    )
    raw_output: str = Field(default="", description="Raw LLM response text.")
    error: str = Field(default="", description="Error message if the call failed.")


__all__ = ["ModelDelegationPathResult"]
