# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelHostileReviewerCompletedEvent — emitted when the hostile reviewer finishes.

Output shape preserved verbatim from the deleted node_hostile_reviewer shell
(OMN-13210 / B1) so the terminal topic
``onex.evt.omnimarket.hostile-reviewer-completed.v1`` round-trips unchanged
across the rebuild.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer_orchestrator.models.model_hostile_reviewer_phase import (
    EnumHostileReviewerPhase,
)


class ModelHostileReviewerCompletedEvent(BaseModel):
    """Final event when the hostile reviewer finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    final_phase: EnumHostileReviewerPhase = Field(...)
    started_at: datetime = Field(...)
    completed_at: datetime = Field(...)
    pass_count: int = Field(default=0, ge=0)
    total_findings: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)
    models_succeeded: list[str] = Field(
        default_factory=list,
        description=(
            "Logical route keys whose inference call completed without error "
            "(OMN-15152). Additive field with a default so replayed events "
            "recorded before this field existed still parse."
        ),
    )
    models_failed: list[str] = Field(
        default_factory=list,
        description=(
            "Logical route keys whose inference call raised (OMN-15152). "
            "Callers use models_succeeded/models_failed to enforce quorum "
            "instead of trusting a bare SUCCESS/0-findings verdict."
        ),
    )


__all__: list[str] = ["ModelHostileReviewerCompletedEvent"]
