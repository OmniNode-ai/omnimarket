# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay, alarm, state and result of the host repair dispatch (OMN-23389)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Level = Literal["ok", "warn", "page"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHostRepairOverlay(_Frozen):
    """Every number and command of the dispatch. Nothing here is a literal of the handler."""

    #: The alarm this node consumes; one repair lane per host and this alarm name.
    alarm_name: NonEmptyStr
    #: Dispatches per host and alarm, the first included. Past it the node escalates once and dispatches no more.
    retry_budget: int = Field(ge=1)
    #: Seconds after attempt ``n`` before the alarm may dispatch attempt ``n + 1``; the last entry repeats.
    backoff_seconds: tuple[int, ...] = Field(min_length=1)
    #: The most seconds from the alarm's reading to the dispatch; a later dispatch is reported as late.
    alarm_to_repair_latency_seconds: int = Field(gt=0)
    #: The durable dedupe state (a leading ``$HOME/`` is the running user's home).
    state_file: NonEmptyStr
    repair_ticket: NonEmptyStr
    parent_lane: NonEmptyStr
    lane_prefix: NonEmptyStr
    #: The lane brief; ``{host}``, ``{attempt}``, ``{budget}``, ``{alarm}``, ``{read_at}``, ``{used_percent}``,
    #: ``{df}`` and ``{findmnt}`` are replaced.
    brief_template: NonEmptyStr
    #: argv of the command that measures a host, ``{host}`` replaced; its stdout is split at ``separator`` into the
    #: df reading and the findmnt reading.
    measure_argv: tuple[NonEmptyStr, ...] = Field(min_length=1)
    measure_separator: NonEmptyStr
    measure_timeout_seconds: int = Field(gt=0)
    #: argv of the command that starts the repair lane; ``{lane}``, ``{brief_file}``, ``{target}``, ``{ticket}``,
    #: ``{parent}`` and ``{ledger}`` are replaced.
    dispatch_argv: tuple[NonEmptyStr, ...] = Field(min_length=1)
    dispatch_timeout_seconds: int = Field(gt=0)
    #: argv of the command that writes the escalation row; ``{row}`` is replaced.
    escalate_argv: tuple[NonEmptyStr, ...] = Field(min_length=1)
    brief_dir: NonEmptyStr

    @model_validator(mode="after")
    def backoff_is_positive(self) -> Self:
        if any(step < 0 for step in self.backoff_seconds):
            raise ValueError("backoff_seconds must not be negative")
        return self


class _Reading(BaseModel):
    """The watermark node's reading file carries more than the alarm needs; the rest is ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class ModelHostVolumeAlarm(_Reading):
    path: NonEmptyStr
    used_percent: float
    page_percent: int


class ModelHostDiskAlarm(_Reading):
    """The watermark node's reading as the alarm event: ``level`` ``page`` is the alarm, any other level clears it."""

    host_name: NonEmptyStr
    read_at: NonEmptyStr
    level: Level
    volumes: tuple[ModelHostVolumeAlarm, ...] = ()


class ModelHostRepairRequest(_Frozen):
    alarm: ModelHostDiskAlarm
    overlay_file: str = ""
    #: The consumer's clock reading, ISO 8601 UTC; empty reads the handler's own clock.
    now: str = ""
    #: The ledger the started lane writes its rows to; passed to the dispatch command as ``{ledger}``.
    ledger_file: str = ""


class EnumHostRepairAction(StrEnum):
    DISPATCHED = "dispatched"
    RETRIED = "retried"
    DEDUPED = "deduped"
    ESCALATED = "escalated"
    ESCALATED_ALREADY = "escalated-already"
    MEASURE_FAILED = "measure-failed"
    DISPATCH_FAILED = "dispatch-failed"
    CLEARED = "cleared"
    IGNORED = "ignored"


class ModelRepairState(_Frozen):
    """The durable record of one host and alarm."""

    key: NonEmptyStr
    #: The first alarm's reading time of the episode; it names the episode's lanes.
    episode: NonEmptyStr
    attempts: int = Field(ge=0)
    last_dispatch_at: NonEmptyStr
    lane: str = ""
    escalated: bool = False
    last_error: str = ""


class ModelHostRepairResult(_Frozen):
    action: EnumHostRepairAction
    key: str
    host_name: str
    attempt: int = 0
    lane: str = ""
    #: Seconds from the alarm's reading to this dispatch, None when nothing was dispatched.
    latency_seconds: int | None = None
    latency_exceeded: bool = False
    brief_file: str = ""
    reason: str = ""


class ModelHostMeasurement(_Frozen):
    df: NonEmptyStr
    findmnt: NonEmptyStr


class ModelRepairLaneSpec(_Frozen):
    lane: NonEmptyStr
    target: NonEmptyStr
    ticket: NonEmptyStr
    parent: NonEmptyStr
    brief: NonEmptyStr
    brief_file: NonEmptyStr
    ledger: str = ""
