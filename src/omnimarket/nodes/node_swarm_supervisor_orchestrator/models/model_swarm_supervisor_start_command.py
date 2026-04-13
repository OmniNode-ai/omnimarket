# SPDX-License-Identifier: MIT
"""ModelSwarmSupervisorStartCommand — start command for the swarm supervisor."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelSwarmSupervisorStartCommand(BaseModel):
    """Command to start a swarm supervisor session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Unique supervisor session ID.")
    poll_interval_seconds: int = Field(
        default=300,
        description="Heartbeat poll interval in seconds.",
        ge=10,
    )
    zombie_threshold_seconds: int = Field(
        default=600,
        description="Seconds of silence before a worker is classified as zombie.",
        ge=60,
    )
    context_exhaustion_pct: float = Field(
        default=0.80,
        description="Context usage fraction (0.0-1.0) that triggers respawn.",
        ge=0.0,
        le=1.0,
    )
    max_respawn_attempts: int = Field(
        default=3,
        description="Maximum respawn attempts per worker before abandoning.",
        ge=1,
    )
    dry_run: bool = Field(
        default=False,
        description="Log respawn decisions without actually spawning.",
    )
    worker_ids: list[str] = Field(
        default_factory=list,
        description="Worker task IDs to supervise. Empty list = all active.",
    )


__all__: list[str] = ["ModelSwarmSupervisorStartCommand"]
