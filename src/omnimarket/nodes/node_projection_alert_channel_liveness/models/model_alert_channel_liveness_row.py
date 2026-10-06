# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable row one completed alert-channel probe becomes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelAlertChannelLivenessRow(BaseModel):
    """One measured verdict, ready to write.

    No row exists for a throttled tick. The ingest clock and database-assigned
    cursor belong to the writer and migration, respectively; the pure fold
    never manufactures either. An absent checked_at is resolved to that same
    ingest clock by the writer, so there is only one fallback time per row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Deterministic delivery identity, or content-derived fallback."
    )
    status: str = Field(..., min_length=1, description="The measured status token.")
    healthy: bool = Field(..., description="True only for a LIVE verdict.")
    reason: str = Field(..., description="The probe's own explanation, verbatim.")
    slack_error: str | None = Field(default=None, description="Slack's error token.")
    probe_interval_seconds: int = Field(
        ..., gt=0, description="The interval this measurement was produced under."
    )
    failure_surfaced: bool = Field(
        ..., description="Whether the producer independently surfaced the failure."
    )
    checked_at: datetime | None = Field(
        default=None, description="Event time; absent means use projected_at on write."
    )
    source_topic: str = Field(default="", description="Consumed topic, for provenance.")


__all__ = ["ModelAlertChannelLivenessRow"]
