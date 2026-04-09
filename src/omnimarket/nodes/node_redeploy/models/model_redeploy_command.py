"""ModelRedeployCommand — command to start post-release redeploy."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelRedeployCommand(BaseModel):
    """Command to start a post-release runtime redeploy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    versions: dict[str, str] = Field(
        default_factory=dict,
        description="Plugin version pins (pkg -> version).",
    )
    skip_sync: bool = Field(default=False, description="Skip SYNC_CLONES phase.")
    verify_only: bool = Field(default=False, description="Skip to VERIFY_HEALTH only.")
    dry_run: bool = Field(default=False, description="Print without executing.")
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelRedeployCommand"]
