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
    failure_reasons: tuple[str, ...] = Field(
        default=(),
        description="Failure reason strings emitted by the quality gate.",
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
