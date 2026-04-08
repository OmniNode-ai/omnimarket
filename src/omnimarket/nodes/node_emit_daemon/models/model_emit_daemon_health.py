# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Emit daemon health model.

Returned by the health endpoint. Designed to be constructed and serialized
within the 100ms health budget -- no blocking I/O in any field computation.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_emit_daemon.models.model_emit_daemon_config import (
    EnumCircuitBreakerState,
)


class ModelEmitDaemonHealth(BaseModel):
    """Health snapshot of the emit daemon."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Overall status
    healthy: bool = Field(
        ..., description="True when daemon is listening and circuit is not OPEN"
    )

    # Circuit breaker
    circuit_state: EnumCircuitBreakerState = Field(
        ..., description="Current circuit breaker state"
    )
    consecutive_failures: int = Field(default=0, ge=0)

    # Queue depth
    memory_queue_size: int = Field(default=0, ge=0)
    spool_queue_size: int = Field(default=0, ge=0)

    # Counters
    events_published: int = Field(default=0, ge=0)
    events_dropped: int = Field(default=0, ge=0)
    events_buffered: int = Field(
        default=0,
        ge=0,
        description="Events buffered during circuit OPEN state",
    )

    # Timestamps
    last_publish_at: datetime | None = Field(
        default=None, description="Timestamp of last successful Kafka publish"
    )
    last_failure_at: datetime | None = Field(
        default=None, description="Timestamp of last publish failure"
    )
    circuit_opened_at: datetime | None = Field(
        default=None, description="When circuit transitioned to OPEN"
    )
    uptime_seconds: float = Field(
        default=0.0, ge=0.0, description="Seconds since daemon started"
    )

    # Kafka connectivity
    kafka_connected: bool = Field(
        default=False, description="Whether Kafka publish_fn is wired (not no-op)"
    )


__all__: list[str] = ["ModelEmitDaemonHealth"]
