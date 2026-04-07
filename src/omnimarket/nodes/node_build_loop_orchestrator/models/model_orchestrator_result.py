"""ModelOrchestratorResult -- final result from the build loop orchestrator.

Related:
    - OMN-7583: Migrate build loop orchestrator
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

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


__all__: list[str] = ["ModelOrchestratorResult"]
