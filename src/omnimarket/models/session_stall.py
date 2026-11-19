# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the orchestrating-session stall alarm (OMN-25405).

The judge reads the queue rows it is given and ``now``. A reader outside the judge turns the session transcript's
``queue-operation`` rows (enqueue, dequeue, remove) and the scheduled-task firings into :class:`ModelQueueRow`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryResult


class EnumQueueOperation(StrEnum):
    #: Input arrived for the session and waits in its queue.
    ENQUEUE = "enqueue"
    #: The session took the oldest queued input.
    DEQUEUE = "dequeue"
    #: Queued input was dropped before the session took it.
    REMOVE = "remove"
    #: A scheduled task (a recurring session tick) fired.
    TASK_FIRED = "task_fired"


class EnumSessionStallReason(StrEnum):
    OLDEST_ENQUEUE_UNDEQUEUED = "oldest-enqueue-undequeued"
    NO_SCHEDULED_TASK = "no-scheduled-task"


class EnumSessionStallOutcome(StrEnum):
    OK = "ok"
    STALLED = "stalled"
    #: The transcript could not be read: neither a stall nor a recovery.
    INDETERMINATE = "indeterminate"


class ModelQueueRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    operation: EnumQueueOperation

    @field_validator("at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("a queue row's time carries its zone")
        return value


class ModelSessionStallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    rows: tuple[ModelQueueRow, ...] = ()
    #: A recurring session tick is declared: only then is a quiet scheduler a stall.
    tick_declared: bool
    #: The oldest queued enqueue waits this long with no dequeue: a stall (ticket: 15 minutes).
    dequeue_after_seconds: int = Field(default=900, ge=1)
    #: No scheduled task fired for this long while a tick is declared: a stall (ticket: 45 minutes).
    task_after_seconds: int = Field(default=2700, ge=1)


class ModelSessionStallVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stalled: bool
    reasons: tuple[EnumSessionStallReason, ...] = ()
    #: Enqueues with no dequeue or remove yet, as of ``now``.
    queued: int = 0
    oldest_enqueue_at: datetime | None = None
    last_task_at: datetime | None = None
    #: What a reader can act on; blank when not stalled.
    detail: str = ""
    #: The reading itself, quoted in the ledger row; blank when not stalled.
    evidence: str = ""


class ModelSessionStallTickRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: False judges and delivers nothing (a probe).
    write: bool = True


class ModelSessionStallTickResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumSessionStallOutcome
    #: None when the transcript could not be read.
    verdict: ModelSessionStallVerdict | None
    delivery: ModelLabAlarmDeliveryResult
    #: Why the transcript was not read; blank otherwise.
    failure: str = ""


__all__ = [
    "EnumQueueOperation",
    "EnumSessionStallOutcome",
    "EnumSessionStallReason",
    "ModelQueueRow",
    "ModelSessionStallRequest",
    "ModelSessionStallTickRequest",
    "ModelSessionStallTickResult",
    "ModelSessionStallVerdict",
]
