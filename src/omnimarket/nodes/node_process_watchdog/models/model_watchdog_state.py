"""Process watchdog enums and state models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumCheckStatus(StrEnum):
    """Health status for a single check target."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


class EnumCheckTarget(StrEnum):
    """Known check target categories."""

    EMIT_DAEMON = "emit_daemon"
    KAFKA_CONSUMERS = "kafka_consumers"
    LLM_ENDPOINTS = "llm_endpoints"
    DOCKER_CONTAINERS = "docker_containers"
    UNIX_SOCKET = "unix_socket"


class ModelWatchdogCheckResult(BaseModel):
    """Result of a single health check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: str = Field(..., description="Check target identifier.")
    category: EnumCheckTarget = Field(..., description="Check category.")
    status: EnumCheckStatus = Field(..., description="Health status.")
    message: str = Field(default="", description="Human-readable status message.")
    details: dict[str, object] = Field(
        default_factory=dict,
        description="Structured details (latency, lag, container info, etc.).",
    )
    restart_attempted: bool = Field(
        default=False, description="Whether an auto-restart was attempted."
    )
    restart_succeeded: bool | None = Field(
        default=None,
        description="Whether the restart succeeded (None if not attempted).",
    )


class ModelWatchdogReport(BaseModel):
    """Aggregated watchdog report across all check targets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall_status: EnumCheckStatus = Field(
        ..., description="Worst status across all checks."
    )
    checks: list[ModelWatchdogCheckResult] = Field(
        default_factory=list, description="Individual check results."
    )
    total_checks: int = Field(default=0, ge=0)
    healthy_count: int = Field(default=0, ge=0)
    degraded_count: int = Field(default=0, ge=0)
    down_count: int = Field(default=0, ge=0)
    unknown_count: int = Field(default=0, ge=0)
    alerts_emitted: int = Field(default=0, ge=0)
    restarts_attempted: int = Field(default=0, ge=0)
    correlation_id: str = Field(...)
    dry_run: bool = Field(default=False)


__all__: list[str] = [
    "EnumCheckStatus",
    "EnumCheckTarget",
    "ModelWatchdogCheckResult",
    "ModelWatchdogReport",
]
