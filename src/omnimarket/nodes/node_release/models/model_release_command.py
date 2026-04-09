"""ModelReleaseCommand — command to start the release pipeline."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelReleaseCommand(BaseModel):
    """Command to start a coordinated multi-repo release."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Release run correlation ID.")
    repos: list[str] = Field(
        default_factory=list, description="Repos to release (empty = all)."
    )
    bump: str | None = Field(
        default=None, description="Override bump level: major | minor | patch."
    )
    dry_run: bool = Field(default=False, description="Show plan without executing.")
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelReleaseCommand"]
