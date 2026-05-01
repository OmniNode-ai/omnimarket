# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed result for the emit daemon end-to-end health probe."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelDaemonHealthProbeResult(BaseModel):
    """Result of a synthetic emit-daemon socket-to-Kafka round trip."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    correlation_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    socket_path: str = Field(..., min_length=1)
    bootstrap_servers: str = Field(..., min_length=1)
    topic: str | None = Field(default=None)
    event_id: str | None = Field(default=None)
    kafka_offset: int | None = Field(default=None)
    round_trip_ms: float | None = Field(default=None, ge=0.0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__: list[str] = ["ModelDaemonHealthProbeResult"]
