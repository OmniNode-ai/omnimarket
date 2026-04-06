# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Emit daemon lifecycle FSM state, command, and event models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumEmitDaemonPhase(StrEnum):
    """Lifecycle phases for the emit daemon FSM."""

    IDLE = "idle"
    BINDING = "binding"
    LISTENING = "listening"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


class ModelEmitDaemonState(BaseModel):
    """Current state of the emit daemon."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: EnumEmitDaemonPhase = Field(default=EnumEmitDaemonPhase.IDLE)
    socket_path: str | None = Field(default=None)
    pid: int | None = Field(default=None)
    events_queued: int = Field(default=0, ge=0)
    events_published: int = Field(default=0, ge=0)
    events_dropped: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    started_at: datetime | None = Field(default=None)
    error: str | None = Field(default=None)


class ModelEmitDaemonCommand(BaseModel):
    """Command to start or stop the emit daemon."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str = Field(..., pattern=r"^(start|stop|health)$")
    socket_path: str | None = Field(default=None)
    kafka_bootstrap_servers: str | None = Field(default=None)
    event_registry_path: str | None = Field(default=None)


class ModelEmitDaemonCompletedEvent(BaseModel):
    """Event emitted when the daemon completes a lifecycle transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: EnumEmitDaemonPhase = Field(...)
    previous_phase: EnumEmitDaemonPhase = Field(...)
    events_published: int = Field(default=0, ge=0)
    events_dropped: int = Field(default=0, ge=0)
    error: str | None = Field(default=None)


__all__: list[str] = [
    "EnumEmitDaemonPhase",
    "ModelEmitDaemonCommand",
    "ModelEmitDaemonCompletedEvent",
    "ModelEmitDaemonState",
]
