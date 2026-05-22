"""Projection result models for the evidence dashboard reducer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelEvidenceDashboardReductionResult(BaseModel):
    """Summary of reducer writes for one normalized event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    tables: tuple[str, ...] = Field(default_factory=tuple)
    projection_cursor: str
    last_event_id: str
