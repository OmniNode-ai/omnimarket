"""ModelHostileReviewerStartCommand — command to start the hostile reviewer."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelHostileReviewerStartCommand(BaseModel):
    """Command to start the hostile reviewer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Review run correlation ID.")
    pr_number: int | None = Field(default=None, description="PR number to review.")
    repo: str | None = Field(default=None, description="GitHub repo (owner/repo).")
    file_path: str | None = Field(
        default=None, description="File path to review (alternative to PR)."
    )
    models: list[str] = Field(
        ...,
        min_length=1,
        description="Caller-provided logical model route keys to use for review.",
    )
    max_passes: int = Field(default=10, ge=1, description="Max review passes.")
    dry_run: bool = Field(default=False)
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelHostileReviewerStartCommand"]
