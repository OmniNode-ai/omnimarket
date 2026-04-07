"""ModelPhaseCommandIntent — intent emitted by the orchestrator to dispatch a phase."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    EnumBuildLoopPhase,
)


class ModelPhaseCommandIntent(BaseModel):
    """Intent emitted by the orchestrator to command execution of a build loop phase.

    The orchestrator emits intents (not results) — downstream nodes consume
    these to perform actual phase work.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Orchestration correlation ID.")
    target_phase: EnumBuildLoopPhase = Field(
        ..., description="Build loop phase to execute."
    )
    dry_run: bool = Field(default=False, description="No side effects if true.")
    dispatched_at: datetime = Field(..., description="When the intent was dispatched.")


__all__: list[str] = ["ModelPhaseCommandIntent"]
