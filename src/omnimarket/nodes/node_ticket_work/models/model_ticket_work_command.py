"""ModelTicketWorkCommand — command to start per-ticket execution."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelTicketWorkCommand(BaseModel):
    """Command to start per-ticket execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID (e.g., OMN-1807).")
    autonomous: bool = Field(default=False, description="Skip human gates if true.")
    dry_run: bool = Field(default=False, description="No side effects if true.")
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelTicketWorkCommand"]
