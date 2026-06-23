# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Evidence record for a single tier attempt during delegation escalation (OMN-12255)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationEscalationAttempt(BaseModel):
    """Evidence record for a single tier attempt during escalation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_name: str = Field(
        ..., description="Name of the tier attempted (e.g. 'local', 'cheap_cloud')."
    )
    model_used: str = Field(..., description="Model identifier used for this attempt.")
    quality_score: float = Field(
        ..., description="Quality gate score for this attempt (0.0-1.0)."
    )
    required_bar: float | None = Field(
        default=None,
        description="Required adequacy bar resolved from task-class/workflow/request authority.",
    )
    actual_score: float | None = Field(
        default=None,
        description="Actual score compared against required_bar for escalation.",
    )
    authority_source: str | None = Field(
        default=None,
        description="Authority source that supplied required_bar.",
    )
    score_source: str | None = Field(
        default=None,
        description="Scoring authority that produced actual_score.",
    )
    failure_reasons: tuple[str, ...] = Field(
        default=(),
        description="Failure reason strings emitted by the quality gate.",
    )
    # OMN-13535: per-attempt served usage + measured metered cost. On a metered
    # tier that is ATTEMPTED-but-rejected (quality gate fails) and escalates to a
    # cheaper/free tier, the metered call still ran and incurred real tokens/cost.
    # Recording them here keeps each attempt's spend auditable and lets the
    # terminal report the cumulative metered cost across all attempted tiers,
    # instead of dropping the rejected metered tier's cost (the terminal otherwise
    # reflects only the final accepted tier — free → cost_usd=0).
    prompt_tokens: int = Field(
        default=0,
        ge=0,
        description="Served prompt tokens this attempt's inference call reported.",
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        description="Served completion tokens this attempt's inference call reported.",
    )
    cost_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Measured metered cost (USD) for this attempt's served tokens.",
    )
    latency_ms: int = Field(
        ..., description="End-to-end latency for this attempt in milliseconds."
    )
    fallback_recommended: bool = Field(
        ...,
        description="Whether the quality gate recommended fallback to a higher tier.",
    )
    attempted_at: datetime = Field(
        ..., description="Timestamp of the gate evaluation for this attempt."
    )
    routing_decision_id: UUID | None = Field(
        default=None,
        description="ID of the ModelRoutingDecision that produced this attempt, for cross-event correlation.",
    )


__all__: list[str] = ["ModelDelegationEscalationAttempt"]
