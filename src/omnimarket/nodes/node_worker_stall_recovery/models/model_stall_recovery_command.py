"""ModelStallRecoveryCommand — input model for the worker stall recovery node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelStallRecoveryCommand(BaseModel):
    """Command to check agent health and recover if stalled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket ID (e.g., OMN-1234).")
    agent_id: str = Field(..., description="Agent/task ID to monitor/recover.")
    timeout_minutes: int = Field(
        default=2, description="Minutes of inactivity before stall detection."
    )
    context_threshold_pct: int = Field(
        default=80, description="Context usage % that triggers preemptive recovery."
    )
    max_redispatches: int = Field(
        default=2, description="Max redispatch attempts before escalation."
    )
    dry_run: bool = Field(
        default=False, description="If true, check health without taking action."
    )


__all__ = ["ModelStallRecoveryCommand"]
