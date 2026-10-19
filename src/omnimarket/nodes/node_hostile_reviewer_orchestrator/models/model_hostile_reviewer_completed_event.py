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

from omnimarket.models.model_aggregated_finding import ModelAggregatedFinding
from omnimarket.models.model_inference_provenance import ModelInferenceProvenance
from omnimarket.models.model_review_target_snapshot import ModelReviewTargetSnapshot
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
    verdict: str | None = Field(
        default=None,
        description=(
            "The aggregator's verdict over the merged findings: clean, "
            "risks_noted or blocking_issue. None when no review output was "
            "produced (a failed run), so a consumer never reads a failure as clean."
        ),
    )
    findings: list[ModelAggregatedFinding] = Field(
        default_factory=list,
        description=(
            "The deduplicated findings with their source models, severity and "
            "weighted score, so a gate consumes the findings rather than a count."
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
    target_snapshot: ModelReviewTargetSnapshot | None = Field(
        default=None,
        description=(
            "The head sha, base sha and diff digest the review was read against "
            "(OMN-21189), or an unavailable snapshot with the reason. None only on "
            "an event recorded before the field existed or a run that failed before "
            "the target was resolved."
        ),
    )
    route_provenance: dict[str, ModelInferenceProvenance] = Field(
        default_factory=dict,
        description=(
            "Per requested route key: the served model, settings and token usage "
            "the inference result reported, each 'unknown' where it reported "
            "nothing (OMN-21189). A failed route reads all unknown. When "
            "fallback_engine ran, its route key carries the fallback's provenance."
        ),
    )
    fallback_engine: str | None = Field(
        default=None,
        description=(
            "Route key of the fallback engine that produced the review when no "
            "requested route did (OMN-17427); None when the requested routes "
            "reviewed. Additive field with a default so replayed events recorded "
            "before this field existed still parse."
        ),
    )
    fallback_for: list[str] = Field(
        default_factory=list,
        description=(
            "Requested route keys that produced no review output and were "
            "replaced by fallback_engine (OMN-17427)."
        ),
    )


__all__: list[str] = ["ModelHostileReviewerCompletedEvent"]
