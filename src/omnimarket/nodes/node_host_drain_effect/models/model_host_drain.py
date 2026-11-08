# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the drain verb and the idle-only runner stop (rebuild plan 7.4, OMN-22905)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumDrainOp(StrEnum):
    """What a drain command asks for."""

    DRAIN = "drain"
    UNDRAIN = "undrain"
    STATUS = "status"
    CHECK_START = "check_start"


class EnumDrainOutcome(StrEnum):
    DRAINING = "draining"
    CLEARED = "cleared"
    STATUS_READ = "status_read"
    START_ADMITTED = "start_admitted"
    START_REFUSED = "start_refused"
    FAILED = "failed"


class EnumRunnerActivity(StrEnum):
    """What a runner's own process table says: a worker process means it holds a job."""

    IDLE = "idle"
    BUSY = "busy"
    UNKNOWN = "unknown"


class EnumRunnerStopOutcome(StrEnum):
    STOPPED = "stopped"
    REFUSED_BUSY = "refused_busy"
    REFUSED_UNREADABLE = "refused_unreadable"
    FAILED = "failed"


class ModelHostDrainRequest(_Frozen):
    op: EnumDrainOp
    host_name: str = Field(min_length=1)
    #: Chain id of this command. Empty: the handler mints one and returns it.
    correlation_id: str = ""
    reason: str = ""


class ModelHostDrainResult(_Frozen):
    outcome: EnumDrainOutcome
    host_name: str
    correlation_id: str
    draining: bool = False
    #: Lanes still running on the host. A drain never stops them; the host is drained when this reads 0.
    #: None when the count could not be read: an unknown count is never a measured zero.
    lanes_running: int | None = Field(default=0, ge=0)
    drained: bool = False
    #: Only a start check sets this: True when a new lane may be placed on the host.
    admitted: bool | None = None
    failure_code: str = ""
    detail: str = ""


class ModelIdleRunnerStopRequest(_Frozen):
    runner_name: str = Field(min_length=1)
    correlation_id: str = ""


class ModelIdleRunnerStopResult(_Frozen):
    outcome: EnumRunnerStopOutcome
    runner_name: str
    correlation_id: str
    activity: EnumRunnerActivity = EnumRunnerActivity.UNKNOWN
    failure_code: str = ""
    detail: str = ""


__all__ = [
    "EnumDrainOp",
    "EnumDrainOutcome",
    "EnumRunnerActivity",
    "EnumRunnerStopOutcome",
    "ModelHostDrainRequest",
    "ModelHostDrainResult",
    "ModelIdleRunnerStopRequest",
    "ModelIdleRunnerStopResult",
]
