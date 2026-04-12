# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Health status model for intent event consumer."""

from datetime import UTC, datetime
from typing import Literal

from omnimemory.models.utils.model_health_status import HealthStatus
from pydantic import BaseModel, ConfigDict, Field


# omnimemory-model-exempt: handler health
class ModelIntentEventConsumerHealth(BaseModel):
    """Health status for the intent event consumer.

    Avoids "always returns OK" anti-pattern by tracking:
    - Initialization state
    - Circuit breaker state
    - Staleness (time since last consumption)
    - Dependency health (storage handler)
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    # Overall status
    status: HealthStatus = Field(..., description="Overall health status")
    is_healthy: bool = Field(..., description="Whether consumer is healthy")
    initialized: bool = Field(..., description="Whether consumer is initialized")
    error_message: str | None = Field(
        default=None, description="Error details if unhealthy"
    )

    # Timestamp tracking
    health_check_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this health check was performed",
    )
    last_consume_timestamp: datetime | None = Field(
        default=None,
        description="Timestamp of last successfully consumed message",
    )

    # Staleness detection
    is_stale: bool = Field(
        default=False,
        description="Whether consumer is stale (no messages within threshold)",
    )
    staleness_seconds: float | None = Field(
        default=None,
        ge=0,
        description="Seconds since last message consumption",
    )

    # Circuit breaker status
    circuit_breaker_state: Literal["closed", "open", "half_open"] | None = Field(
        default=None,
        description="Circuit breaker state",
    )
    circuit_breaker_failure_count: int = Field(
        default=0,
        ge=0,
        description="Current failure count in circuit breaker",
    )

    # Metrics
    messages_consumed_total: int = Field(
        default=0,
        ge=0,
        description="Total messages successfully consumed and stored",
    )
    messages_failed_total: int = Field(
        default=0,
        ge=0,
        description="Total messages that failed processing",
    )
    messages_dlq_total: int = Field(
        default=0,
        ge=0,
        description="Total messages routed to DLQ",
    )

    # Dependency health
    storage_handler_healthy: bool | None = Field(
        default=None,
        description="Health of the underlying intent storage handler",
    )
