"""ModelPipelineStartCommand — command to start the ticket pipeline."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPipelineStartCommand(BaseModel):
    """Command to start the ticket pipeline for a given ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID (e.g. OMN-1234).")
    skip_test_iterate: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    skip_to: str | None = Field(
        default=None, description="Resume from specified phase."
    )
    requested_at: datetime = Field(..., description="When the command was issued.")
    source_correlation_id: str | None = Field(
        default=None,
        description=(
            "Upstream correlation ID from the dispatcher that produced this run "
            "(e.g. build_loop cycle). Rendered into the PR body and Linear comment "
            "so a merged PR can be traced back to its originating cycle."
        ),
    )
    source: str | None = Field(
        default=None,
        description="Name of the upstream dispatcher (e.g. 'build_loop').",
    )


__all__: list[str] = ["ModelPipelineStartCommand"]
