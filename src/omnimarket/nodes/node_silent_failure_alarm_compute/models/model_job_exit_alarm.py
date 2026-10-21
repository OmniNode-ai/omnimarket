# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Launchd readings, persisted streaks and job-exit decisions (OMN-19998, OMN-17427).

An episode has a cause: ``failed-exit`` (the job's runs exited nonzero) or ``missed-interval``
(its run counter did not grow for twice its interval). Each job and cause keeps the time it last
opened an episode, so a second episode of the same pair waits out the cooldown.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

JobExitCause = Literal["failed-exit", "missed-interval"]


class ModelJobExitReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    runs: int | None = None
    last_exit: int | None = None
    interval_s: int | None = Field(default=None, gt=0)
    stdout_path: str | None = None
    stderr_path: str | None = None


class ModelJobExitStreak(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    runs: int | None = None
    streak: int = Field(default=0, ge=0)
    last_exit: int | None = None
    episode: str | None = None
    opened_at: AwareDatetime | None = None
    pushed: bool = False
    dispatched: bool = False
    cause: JobExitCause | None = None
    last_run_at: AwareDatetime | None = None
    dispatched_at: dict[str, AwareDatetime] = Field(default_factory=dict)


class ModelJobExitEpisode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    episode: str
    label: str
    lane_kind: str
    streak: int
    last_exit: int | None
    cause: JobExitCause = "failed-exit"
    stdout_path: str | None = None
    stderr_path: str | None = None


class ModelJobExitAlarmRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    readable: bool
    readings: tuple[ModelJobExitReading, ...] = ()
    previous: tuple[ModelJobExitStreak, ...] = ()
    cited_episodes: tuple[str, ...] = ()
    threshold: int = Field(default=3, gt=0)
    push_after_minutes: int = Field(default=30, ge=0)
    lanes: dict[str, str] = Field(default_factory=dict)
    intervals: dict[str, int] = Field(default_factory=dict)
    missed_factor: int = Field(default=2, gt=0)
    cooldown_minutes: int = Field(default=120, ge=0)
    max_opened: int = Field(default=6, gt=0)


class ModelJobExitAlarmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["OK", "FAILING", "UNREADABLE"]
    streaks: tuple[ModelJobExitStreak, ...] = ()
    opened: tuple[ModelJobExitEpisode, ...] = ()
    closed: tuple[str, ...] = ()
    push: tuple[ModelJobExitEpisode, ...] = ()
    failing: tuple[str, ...] = ()
    suppressed: tuple[str, ...] = ()
    friction_cells: tuple[str, ...] = ()
    alert_text: tuple[str, ...] = ()
