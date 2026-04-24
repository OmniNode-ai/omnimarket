"""ModelCloseOutSkipped — emitted when a close-out run is skipped by the guard."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelCloseOutSkipped(BaseModel):
    """Terminal event emitted when the concurrent-run guard rejects an invocation.

    When a second close-out run fires while an earlier run is still holding the
    lease, the guard returns this event instead of a ModelCloseOutCompletedEvent.
    The process must exit 0 — skipping is a first-class non-error outcome.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID of the invocation that was skipped."
    )
    reason: str = Field(
        ...,
        description="Machine-readable skip reason (e.g. 'concurrent_run_in_progress').",
    )
    skipped_at: datetime = Field(
        ..., description="UTC timestamp at which the guard fired."
    )
    holder: str | None = Field(
        default=None,
        description="Identifier of the in-flight run holding the lease, if known.",
    )
    holder_acquired_at: datetime | None = Field(
        default=None,
        description="When the in-flight run acquired the lease, if known.",
    )


__all__: list[str] = ["ModelCloseOutSkipped"]
