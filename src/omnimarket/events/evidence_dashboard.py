"""Shared evidence dashboard event models.

The canonical wire DTO belongs in omnibase_compat. This local model keeps the
omnimarket handler and tests executable until that compat package is published.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

DashboardStage = Literal[
    "TRIGGERED",
    "COLLECTED",
    "EXTRACTED",
    "VALIDATED",
    "OCC_PR",
    "COMPLETED",
    "BLOCKED",
    "READINESS_GATE_STARTED",
    "READINESS_GATE_COMPLETED",
    "READINESS_GATE_BLOCKED",
]
DashboardStatus = Literal[
    "PENDING",
    "IN_FLIGHT",
    "PASSED",
    "FAILED",
    "BLOCKED",
    "STALE",
    "DEGRADED",
]
DashboardSeverity = Literal["INFO", "WARNING", "ERROR", "BLOCKING"]
EvidenceLifecycleState = Literal[
    "PROVISIONAL",
    "VALIDATED",
    "FINALIZED",
    "SUPERSEDED",
    "REJECTED",
]


class ModelDashboardProjectionEvent(BaseModel):
    """Normalized event consumed by the evidence dashboard reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(..., min_length=1)
    causation_id: str | None = Field(default=None, min_length=1)
    source_event_type: str = Field(..., min_length=1)
    normalized_stage: DashboardStage = "BLOCKED"
    normalized_status: DashboardStatus = "DEGRADED"
    severity: DashboardSeverity = "INFO"
    lifecycle_state: EvidenceLifecycleState = "PROVISIONAL"
    source_event_hash: str = Field(..., min_length=1)
    projection_cursor: str = Field(..., min_length=1)
    ingest_sequence: int | None = Field(default=None, ge=0)
    correlation_id: str = Field(..., min_length=1)
    ticket_id: str | None = None
    topic: str = Field(..., min_length=1)
    repo: str | None = None
    pr_number: int | None = Field(default=None, ge=0)
    validation_run_id: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> DashboardStatus:
        return self.normalized_status

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source_topic(self) -> str:
        return self.topic


__all__ = [
    "DashboardSeverity",
    "DashboardStage",
    "DashboardStatus",
    "EvidenceLifecycleState",
    "ModelDashboardProjectionEvent",
]
