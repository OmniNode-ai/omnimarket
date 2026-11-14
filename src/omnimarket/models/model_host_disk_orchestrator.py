# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the host disk orchestrator and of the commands it sends the host disk effect (OMN-23866).

The orchestrator sends one ``ModelHostDiskEvictRequested`` per decided command and receives one
``ModelHostDiskEvictCompleted`` or ``ModelHostDiskEvictFailed`` for it, matched by ``idempotency_key``. A command
that gets neither by its deadline is recorded as a ``no_result_by_deadline`` failure; nothing is silently dropped.
In this step every command carries ``practice_mode`` true, set by the overlay: the effect reports what it would remove
and removes nothing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from omnimarket.models.model_host_disk_decision import (
    ModelHostDiskAlarm,
    ModelHostDiskCommand,
    ModelHostDiskDecision,
)
from omnimarket.models.model_host_disk_reading import DiskClassName
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHostDiskOrchestratorOverlay(ModelScheduleDeclaration):
    """The orchestrator's overlay: cadence, practice mode, result deadline and the alert channel.

    ``alarm_channel`` is the Slack channel id the alarm is posted to, supplied here so the alarm never reads a
    host's env file.
    """

    practice_mode: bool = True
    result_deadline_seconds: int = Field(gt=0)
    alarm_channel: NonEmptyStr
    max_hosts: int = Field(default=256, ge=1, le=4096)
    max_open_runs: int = Field(default=16, ge=1, le=256)


class ModelHostDiskLowReported(_Frozen):
    """The low-disk event: a host reports it is low. It starts a run at once, outside the schedule."""

    host_name: NonEmptyStr
    reported_at: datetime
    free_bytes: int | None = Field(default=None, ge=0)
    detail: str = ""


class ModelHostDiskEvictRequested(_Frozen):
    """The typed command to the host disk effect: one per decided (host, class), keyed by its idempotency key."""

    run_id: NonEmptyStr
    idempotency_key: NonEmptyStr
    practice_mode: bool
    deadline_at: datetime
    command: ModelHostDiskCommand


class ModelHostDiskEvictCompleted(_Frozen):
    """The effect's typed result. In practice mode ``evicted_count`` is what it would have removed."""

    idempotency_key: NonEmptyStr
    host_name: NonEmptyStr
    class_name: DiskClassName
    practice_mode: bool
    evicted_count: int = Field(ge=0)
    bytes_freed: int = Field(ge=0)
    detail: str = ""


class EnumHostDiskFailureReason(StrEnum):
    REFUSED = "refused"
    EFFECT_ERROR = "effect_error"
    NO_RESULT_BY_DEADLINE = "no_result_by_deadline"


class ModelHostDiskEvictFailed(_Frozen):
    """The effect's typed failure, or the orchestrator's record of a deadline passing with no answer."""

    idempotency_key: NonEmptyStr
    host_name: NonEmptyStr
    class_name: DiskClassName
    reason: EnumHostDiskFailureReason
    detail: str = ""


class ModelHostDiskCommandOutcome(_Frozen):
    """One command's end state in a run."""

    idempotency_key: NonEmptyStr
    host_name: NonEmptyStr
    class_name: DiskClassName
    status: Literal["completed", "failed"]
    failure_reason: EnumHostDiskFailureReason | None = None
    evicted_count: int = Field(ge=0, default=0)
    bytes_freed: int = Field(ge=0, default=0)
    detail: str = ""


class ModelHostDiskReadingSummary(_Frozen):
    """What the run read for one host."""

    host_name: NonEmptyStr
    read_at: datetime
    free_bytes: int | None = None


class EnumHostDiskRunOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    NOTHING_DUE = "nothing_due"
    POLICY_REFUSED = "policy_refused"


class ModelHostDiskRunCompleted(_Frozen):
    """The run record the projection keeps: reading, decision, command and result."""

    run_id: NonEmptyStr
    trigger: Literal["scheduled", "low_disk"]
    started_at: datetime
    finished_at: datetime
    practice_mode: bool
    outcome: EnumHostDiskRunOutcome
    readings: tuple[ModelHostDiskReadingSummary, ...] = ()
    decision: ModelHostDiskDecision | None = None
    commands: tuple[ModelHostDiskEvictRequested, ...] = ()
    results: tuple[ModelHostDiskCommandOutcome, ...] = ()
    alarms_sent: tuple[ModelHostDiskAlarm, ...] = ()
    detail: str = ""


def run_outcome(
    results: tuple[ModelHostDiskCommandOutcome, ...],
) -> EnumHostDiskRunOutcome:
    """Derive the run outcome from its command outcomes."""
    if not results:
        return EnumHostDiskRunOutcome.NOTHING_DUE
    statuses = [r.status for r in results]
    if all(s == "completed" for s in statuses):
        return EnumHostDiskRunOutcome.COMPLETED
    if all(s == "failed" for s in statuses):
        return EnumHostDiskRunOutcome.FAILED
    return EnumHostDiskRunOutcome.PARTIAL


__all__: list[str] = [
    "EnumHostDiskFailureReason",
    "EnumHostDiskRunOutcome",
    "ModelHostDiskCommandOutcome",
    "ModelHostDiskEvictCompleted",
    "ModelHostDiskEvictFailed",
    "ModelHostDiskEvictRequested",
    "ModelHostDiskLowReported",
    "ModelHostDiskOrchestratorOverlay",
    "ModelHostDiskReadingSummary",
    "ModelHostDiskRunCompleted",
    "run_outcome",
]
