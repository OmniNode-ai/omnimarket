"""Models for node_design_to_plan Phase 3 native launch routing."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.design_to_plan import ModelPlanToTicketsStartCommand


class ModelDesignToPlanPhase3Dispatch(BaseModel):
    """One typed Onex-native downstream command produced by Phase 3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route_id: str = Field(..., description="Stable route identifier from contract.")
    target_node: str = Field(..., description="Onex-native node receiving the command.")
    command_topic: str = Field(..., description="Contract-declared command topic.")
    command_model: str = Field(..., description="Fully-qualified command model path.")
    command: ModelPlanToTicketsStartCommand = Field(
        ..., description="Typed downstream command payload."
    )


class ModelDesignToPlanPhase3LaunchResult(BaseModel):
    """Result for Phase 3 launch routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Run correlation ID.")
    status: Literal["ready", "planned", "skipped"] = Field(
        ..., description="Launch routing status."
    )
    plan_path: str | None = Field(default=None)
    dry_run: bool = Field(default=False)
    plan_only: bool = Field(default=False)
    dispatches: tuple[ModelDesignToPlanPhase3Dispatch, ...] = Field(default=())


__all__ = [
    "ModelDesignToPlanPhase3Dispatch",
    "ModelDesignToPlanPhase3LaunchResult",
]
