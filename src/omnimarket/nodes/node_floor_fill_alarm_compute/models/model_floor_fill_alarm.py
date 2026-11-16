# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the alarm reads (the fill's runs and the lanes it started) and the alarm rows it answers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("a naive datetime is refused; give the time with its zone")
    return value


class ModelFloorFillRun(BaseModel):
    """One floor fill run as its stored answer reads: the free slots it saw, the lanes it sent, its cause line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    # What the run itself saw; None when it saw no pool (a run that answered ``UNKNOWN pool-unread:*`` saw no slots).
    free_slots: int | None = Field(default=None, ge=0)
    lanes_sent: int = Field(ge=0)
    # The run's first answer line or its refusal, e.g. ``UNKNOWN pool-unread:unbound:pool``; empty when it named none.
    cause: str = ""

    _observed_at_aware = field_validator("observed_at")(_aware)


class ModelFloorFillSentLane(BaseModel):
    """A lane the fill started, under its ``floor:<line_id>:<task_id>`` key, and when its CLAIM row was read (None: not yet)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    key: str = Field(min_length=1)
    sent_at: datetime
    claim_at: datetime | None = None

    _times_aware = field_validator("sent_at", "claim_at")(_aware)

    @model_validator(mode="after")
    def _claim_follows_the_start(self) -> Self:
        if self.claim_at is not None and self.claim_at < self.sent_at:
            raise ValueError(f"CLAIM for {self.key} is before its lane's start")
        return self


class ModelFloorFillPoolRead(BaseModel):
    """One pool read made by the check itself, apart from the fill's runs: the free slots, or why it could not read.

    ``free_slots`` None is an unread pool and carries its cause; it is never the same answer as 0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    free_slots: int | None = Field(default=None, ge=0)
    unread_cause: str = ""

    @model_validator(mode="after")
    def _unread_names_its_cause(self) -> Self:
        if self.free_slots is None and not self.unread_cause:
            raise ValueError("an unread pool names its cause")
        if self.free_slots is not None and self.unread_cause:
            raise ValueError("a pool that read a count carries no unread cause")
        return self


class ModelFloorFillAlarmRequest(BaseModel):
    """The runs (any order), the started lanes, the time of the check and the rule's bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    runs: tuple[ModelFloorFillRun, ...]
    # The independent pool read the IDLE rule takes its free slots from (the runs' own counts cannot raise it).
    pool: ModelFloorFillPoolRead
    sent_lanes: tuple[ModelFloorFillSentLane, ...] = ()
    slot_threshold: int = Field(default=5, ge=0)
    idle_runs: int = Field(default=2, ge=1)
    claim_deadline_minutes: int = Field(default=10, ge=1)
    # When set, no run observed in this many minutes before ``now`` is its own alarm: the fill is not running at all.
    max_run_age_minutes: int | None = Field(default=None, ge=1)

    _now_aware = field_validator("now")(_aware)

    @model_validator(mode="after")
    def _runs_are_not_from_the_future(self) -> Self:
        late = [run.observed_at for run in self.runs if run.observed_at > self.now]
        if late:
            raise ValueError(
                f"a run observed at {max(late).isoformat()} is after the check at {self.now.isoformat()}"
            )
        return self


class EnumFloorFillAlarmKind(StrEnum):
    NOT_RUNNING = "floor-fill-not-running"
    POOL_UNREAD = "floor-fill-pool-unread"
    IDLE = "floor-fill-idle"
    UNCLAIMED = "floor-fill-unclaimed"


class ModelFloorFillAlarmRow(BaseModel):
    """One alarm: its kind, what it is about (``floor-fill`` or the lane), the cause and the row text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumFloorFillAlarmKind
    subject: str
    cause: str
    row_text: str
    # What a repeat of this alarm shares: the effect writes one row per key until the cause clears.
    dedupe_key: str
    # True for a cause that never clears (a CLAIM that arrived late stays late): the effect writes it once, however long
    # the repeat window is, where any other alarm is written again when the window has passed and the cause stands.
    write_once: bool = False


class ModelFloorFillAlarmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alarms: tuple[ModelFloorFillAlarmRow, ...]
    runs_read: int
    lanes_read: int


__all__: list[str] = [
    "EnumFloorFillAlarmKind",
    "ModelFloorFillAlarmRequest",
    "ModelFloorFillAlarmResult",
    "ModelFloorFillAlarmRow",
    "ModelFloorFillPoolRead",
    "ModelFloorFillRun",
    "ModelFloorFillSentLane",
]
