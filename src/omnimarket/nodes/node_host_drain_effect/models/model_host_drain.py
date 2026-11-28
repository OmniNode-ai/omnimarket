# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the drain verb and the idle-only runner stop (rebuild plan 7.4, OMN-22905)."""

from __future__ import annotations

from datetime import UTC, datetime
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


class EnumHostReadKind(StrEnum):
    """What a host read the overlay declares covers. The host's lanes and units are on its advertisement, not here."""

    MODEL_SERVERS = "model_servers"


class ModelDrainMark(_Frozen):
    """One host's drain record as the store holds it, before the clock judges it."""

    draining: bool
    #: When the mark expires; None: no expiry.
    until: datetime | None = None

    def reads_draining(self, now: datetime) -> bool:
        """Whether the mark reads as draining at ``now``: draining, and not at or past its until."""
        if not self.draining:
            return False
        if self.until is None:
            return True
        until = (
            self.until
            if self.until.tzinfo is not None
            else self.until.replace(tzinfo=UTC)
        )
        return now < until


class ModelHostReadResult(_Frozen):
    """One host read of a status: what was read and the count, None when it could not be read."""

    name: str
    kind: EnumHostReadKind
    count: int | None = Field(default=None, ge=0)


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
    #: How long the drain mark lasts, in seconds. None: the overlay's default ttl is used.
    ttl_seconds: int | None = Field(default=None, ge=1)


class ModelHostDrainResult(_Frozen):
    outcome: EnumDrainOutcome
    host_name: str
    correlation_id: str
    draining: bool = False
    #: Lanes still running on the host. A drain never stops them; the host is drained when this reads 0.
    #: None when the count could not be read: an unknown count is never a measured zero.
    lanes_running: int | None = Field(default=0, ge=0)
    drained: bool = False
    #: The lanes the host's advertisements name as running, so a status names what keeps the host busy.
    lane_names: tuple[str, ...] = ()
    #: Why the host's own guard holds it (heat, memory, swap, slots); empty when it is not held. A held host is HELD, never
    #: a failure and never SAFE for that reason.
    held_reason: str = ""
    #: True only when the host itself was read and showed nothing running: its advertisements (units and lanes) and every
    #: declared host read read 0, twice, on a drain mark the bus carries. Anything unread is False.
    safe: bool = False
    #: The host reads of this status, in the overlay's order; empty when none is declared.
    host_reads: tuple[ModelHostReadResult, ...] = ()
    #: When the drain mark expires: the host reads not draining from here on. None when it is not draining.
    until: datetime | None = None
    #: One line saying whether the host is safe to power down; an unreadable lane count is never safe.
    status_line: str = ""
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
    "EnumHostReadKind",
    "EnumRunnerActivity",
    "EnumRunnerStopOutcome",
    "ModelDrainMark",
    "ModelHostDrainRequest",
    "ModelHostDrainResult",
    "ModelHostReadResult",
    "ModelIdleRunnerStopRequest",
    "ModelIdleRunnerStopResult",
]
