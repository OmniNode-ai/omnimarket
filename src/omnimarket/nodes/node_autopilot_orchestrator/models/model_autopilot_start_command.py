# SPDX-License-Identifier: MIT
"""ModelAutopilotStartCommand — start command for the autopilot orchestrator."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelAutopilotStartCommand(BaseModel):
    """Command to start an autopilot close-out cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Unique pipeline run ID.")
    mode: str = Field(
        default="close-out",
        description="Execution mode: close-out | build.",
    )
    dry_run: bool = Field(default=False, description="No side effects if true.")
    require_gate: bool = Field(
        default=False,
        description="Opt into Slack HIGH_RISK gate before release.",
    )
    autonomous: bool = Field(
        default=True,
        description="Run without human gates.",
    )


__all__: list[str] = ["ModelAutopilotStartCommand"]
