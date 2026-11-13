# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule trigger models: the declaration, the run event, the held state, and the applied report."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_JOB_ID_PATTERN = r"^[a-z][a-z0-9_]*$"


class ModelScheduleDeclaration(BaseModel):
    """The overlay of every scheduled job node: its cadence and grace window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(
        pattern=_JOB_ID_PATTERN,
        description="The job id; the overlay id is the node name.",
    )
    run_interval_seconds: int = Field(gt=0)
    grace_seconds: int = Field(ge=0)


class ModelScheduleBlock(ModelScheduleDeclaration):
    """The schedule part of a job overlay.

    The keys beyond job_id, run_interval_seconds, grace_seconds belong to the job node's own model and are not read here.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")


class ModelScheduleUnreadableJob(BaseModel):
    """A declared job overlay that could not be read: its overlay id and the error detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str = Field(min_length=1)
    detail: str


class ModelScheduleVerdictOverlay(ModelScheduleDeclaration):
    """The verdict effect's own overlay: its cadence plus the overlay ids it judges."""

    job_overlay_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("job_overlay_ids")
    @classmethod
    def _validate_job_overlay_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for job_overlay_id in value:
            if not re.fullmatch(_JOB_ID_PATTERN, job_overlay_id):
                raise ValueError(
                    f"job_overlay_id {job_overlay_id!r} must match {_JOB_ID_PATTERN}"
                )
        return value


class ModelScheduleVerdictRequest(BaseModel):
    """The input to one verdict pass: the now, the window, the declarations, the states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    observed_since: datetime
    declared: tuple[ModelScheduleDeclaration, ...]
    states: tuple[ModelScheduleJobState, ...]
    unreadable: tuple[ModelScheduleUnreadableJob, ...] = ()


class ModelScheduleJobVerdict(BaseModel):
    """The verdict for one scheduled job: its state, last fire, age, and detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    state: Literal[
        "FIRING", "FAILING", "OVERDUE", "NEVER_FIRED", "PENDING", "UNREADABLE"
    ]
    last_fired_at: datetime | None
    age_seconds: int | None
    detail: str


class ModelScheduleVerdictResult(BaseModel):
    """The verdict pass's output: the verdicts, the red flag, and the reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    verdicts: tuple[ModelScheduleJobVerdict, ...]
    red: bool
    reason: str


class ModelScheduleRunEvent(BaseModel):
    """One scheduled job run: when it was scheduled, when it fired, and how it went."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(pattern=_JOB_ID_PATTERN)
    node_name: str = Field(min_length=1)
    tick_id: str
    scheduled_for: datetime = Field(description="The tick's now.")
    fired_at: datetime
    outcome: Literal["completed", "failed"]
    detail: str = ""


class ModelScheduleJobState(BaseModel):
    """The held state of one scheduled job: the last run and its counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    last_fired_at: datetime
    last_scheduled_for: datetime
    last_outcome: Literal["completed", "failed"]
    fire_count: int = Field(ge=1)
    failure_count: int = Field(ge=0)
    last_failure_detail: str = ""


class ModelScheduleRunApplied(BaseModel):
    """What the reducer reports after one run event: applied, or refused as not newer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: bool
    job_id: str
    fire_count: int = Field(ge=0)
    reason: str


__all__ = [
    "ModelScheduleBlock",
    "ModelScheduleDeclaration",
    "ModelScheduleJobState",
    "ModelScheduleJobVerdict",
    "ModelScheduleRunApplied",
    "ModelScheduleRunEvent",
    "ModelScheduleUnreadableJob",
    "ModelScheduleVerdictOverlay",
    "ModelScheduleVerdictRequest",
    "ModelScheduleVerdictResult",
]
