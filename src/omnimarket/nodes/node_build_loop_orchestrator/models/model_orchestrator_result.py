"""ModelOrchestratorResult -- final result from the build loop orchestrator.

Related:
    - OMN-7583: Migrate build loop orchestrator
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    run_id: str = Field(default="", description="Build-loop terminal projection ID.")
    workflow_name: str = Field(
        default="build_loop", description="Build-loop terminal projection workflow."
    )
    event_type: str = Field(
        default="build-loop-orchestrator-completed",
        description="Build-loop terminal projection event type.",
    )
    terminal_event_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
        description="Build-loop terminal projection timestamp.",
    )

    def model_post_init(self, __context: object) -> None:
        """Populate terminal projection fields from the orchestrator result."""
        if not self.run_id:
            object.__setattr__(self, "run_id", str(self.correlation_id))
        if self.cycle_summaries:
            object.__setattr__(
                self,
                "terminal_event_at",
                max(summary.completed_at for summary in self.cycle_summaries),
            )


__all__: list[str] = ["ModelOrchestratorResult"]
