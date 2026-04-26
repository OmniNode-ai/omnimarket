"""ModelOrchestratorResult -- final result from the build loop orchestrator.

Related:
    - OMN-7583: Migrate build loop orchestrator
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from omnimarket.nodes.node_build_loop_orchestrator.models.model_loop_cycle_summary import (
    ModelLoopCycleSummary,
)


class ModelOrchestratorResult(BaseModel):
    """Final result from the build loop orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Root correlation ID.")
    cycles_completed: int = Field(default=0, ge=0, description="Cycles completed.")
    cycles_failed: int = Field(default=0, ge=0, description="Cycles that failed.")
    cycle_summaries: tuple[ModelLoopCycleSummary, ...] = Field(
        default_factory=tuple, description="Per-cycle summaries."
    )
    total_tickets_dispatched: int = Field(
        default=0, ge=0, description="Total tickets dispatched across all cycles."
    )

    @computed_field
    @property
    def run_id(self) -> str:
        """Workflow run identifier used by build_loop terminal projections."""
        return str(self.correlation_id)

    @computed_field
    @property
    def workflow_name(self) -> str:
        """Workflow name used by build_loop terminal projections."""
        return "build_loop"

    @computed_field
    @property
    def event_type(self) -> str:
        """Terminal event discriminator used by build_loop projections."""
        return "build-loop-orchestrator-completed"

    @computed_field
    @property
    def terminal_event_at(self) -> datetime:
        """Timestamp of the final cycle summary, or now for empty results."""
        if self.cycle_summaries:
            return max(summary.completed_at for summary in self.cycle_summaries)
        return datetime.now(tz=UTC)


__all__: list[str] = ["ModelOrchestratorResult"]
