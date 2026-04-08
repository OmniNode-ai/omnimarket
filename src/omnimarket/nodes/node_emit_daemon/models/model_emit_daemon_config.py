# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Emit daemon configuration model.

Declares all runtime settings: socket path, Kafka servers, circuit breaker
thresholds, queue limits, and retry parameters. Loaded from env vars with
ONEX_EMIT_ prefix or passed directly.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumCircuitBreakerState(StrEnum):
    """Circuit breaker states for Kafka connectivity."""

    CLOSED = "closed"  # Normal operation, publishing to Kafka
    OPEN = "open"  # Kafka down, buffering locally
    HALF_OPEN = "half_open"  # Probing Kafka with a single event


def _default_socket_path() -> str:
    """Resolve default socket path via env or XDG or /tmp fallback."""
    import os

    env_path = os.environ.get("ONEX_EMIT_SOCKET_PATH")
    if env_path:
        return env_path
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "onex", "emit.sock")
    return "/tmp/onex-emit.sock"


def _default_spool_dir() -> Path:
    """Resolve default spool directory via env or XDG or /tmp fallback."""
    import os

    env_path = os.environ.get("ONEX_EMIT_SPOOL_DIR")
    if env_path:
        return Path(env_path)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "onex" / "event-spool"
    return Path("/tmp") / "onex-event-spool"


class ModelEmitDaemonConfig(BaseModel):
    """Configuration for the emit daemon node."""

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        validate_default=True,
    )

    # Socket
    socket_path: str = Field(
        default_factory=_default_socket_path,
        description="Unix domain socket path for event ingestion",
    )
    socket_timeout_seconds: float = Field(
        default=5.0, ge=0.1, le=60.0, description="Client read timeout"
    )
    socket_permissions: int = Field(default=0o660, ge=0, le=0o777)

    # Kafka
    kafka_bootstrap_servers: str | None = Field(
        default=None,
        description="Kafka bootstrap servers (host:port). None = no-kafka mode.",
    )
    kafka_client_id: str = Field(
        default="onex-emit-daemon", min_length=1, max_length=255
    )
    kafka_timeout_seconds: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Kafka publish timeout"
    )

    # Queue / spool
    max_payload_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    max_memory_queue: int = Field(default=100, ge=1, le=10_000)
    max_spool_messages: int = Field(default=1000, ge=0, le=100_000)
    max_spool_bytes: int = Field(default=10_485_760, ge=0, le=1_073_741_824)
    spool_dir: Path = Field(
        default_factory=_default_spool_dir,
        description="Disk spool directory for overflow events",
    )

    # Retry
    max_retry_attempts: int = Field(default=3, ge=1, le=10)
    backoff_base_seconds: float = Field(default=1.0, ge=0.1, le=30.0)
    max_backoff_seconds: float = Field(default=60.0, ge=1.0, le=300.0)

    # Circuit breaker
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Consecutive publish failures before circuit opens",
    )
    circuit_breaker_recovery_timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=600.0,
        description="Seconds in OPEN state before probing HALF_OPEN",
    )
    circuit_breaker_half_open_max_probes: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Number of probe publishes in HALF_OPEN before closing",
    )

    # Shutdown
    shutdown_drain_seconds: float = Field(default=10.0, ge=0.0, le=300.0)

    # PID
    pid_path: str | None = Field(
        default=None,
        description="PID file path. None = auto-resolve from socket path.",
    )

    @field_validator("kafka_bootstrap_servers", mode="after")
    @classmethod
    def validate_bootstrap_servers(cls, v: str | None) -> str | None:
        if v is None:
            return None
        servers = v.strip().split(",")
        for server in servers:
            server = server.strip()
            if not server:
                raise ValueError("Bootstrap servers cannot contain empty entries")
            if ":" not in server:
                raise ValueError(
                    f"Invalid bootstrap server format '{server}'. Expected 'host:port'"
                )
        return v.strip()


__all__: list[str] = ["EnumCircuitBreakerState", "ModelEmitDaemonConfig"]
