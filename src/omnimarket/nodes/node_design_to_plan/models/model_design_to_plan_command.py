"""ModelDesignToPlanCommand — command to start the design-to-plan workflow."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDesignToPlanCommand(BaseModel):
    """Command to start the design-to-plan workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Run correlation ID.")
    topic: str = Field(..., description="Topic or problem to brainstorm.")
    plan_path: str | None = Field(
        default=None, description="Existing plan file path to skip brainstorm."
    )
    no_launch: bool = Field(default=False, description="Stop after plan save.")
    dry_run: bool = Field(default=False, description="No side effects if true.")
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelDesignToPlanCommand"]
