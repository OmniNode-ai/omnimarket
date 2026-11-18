# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the shared-blocker escalation judge (OMN-25406).

The caller reads the landing controller's shared causes and the questions already open and
supplies ``now``; the judge reads no file, runs no process and has no clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GatedReading = Literal["healthy", "red", "unread"]
Trigger = Literal["holds-prs-for-hours", "measures-other-env", "gated-env-healthy"]


class ModelSharedCause(BaseModel):
    """One shared cause as the controller records it: the check, its member PRs and what it measures."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(
        description="The controller's cause key (cause:<owner>/<repo>:<hash>)."
    )
    check: str = Field(
        description="The shared check's name as the status board prints it."
    )
    members: tuple[str, ...] = Field(description="Member pull requests, each repo#n.")
    first_seen: datetime = Field(
        description="When the controller first recorded this cause (tz-aware)."
    )
    measured_env: str = Field(
        description="The environment the check measures, e.g. stability."
    )
    gated_env: str = Field(
        description="The environment of the branch the check gates, e.g. dev."
    )
    gated_reading: GatedReading = Field(
        description="What the gated environment reads now."
    )


class ModelSharedBlockerRequest(BaseModel):
    """Causes and open questions at one clock reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    causes: tuple[ModelSharedCause, ...]
    open_questions: tuple[str, ...] = Field(
        default=(),
        description="The qkeys of operator questions that are open (asked, not answered).",
    )
    min_prs: int = Field(default=5, ge=1)
    hold_hours: float = Field(default=2.0, gt=0)


class ModelOperatorQuestion(BaseModel):
    """The one MSG to=operator a cause raises, and the alarm that goes with it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    to: Literal["operator"] = "operator"
    qkey: str
    question: str
    options: tuple[str, ...]
    recommend: str
    members: tuple[str, ...]
    alarm_key: str


class ModelSharedCauseJudgement(BaseModel):
    """What one cause reads now: its age, its PR count, its triggers and whether its question is open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cause_key: str
    check: str
    age_hours: float
    pr_count: int
    triggers: tuple[Trigger, ...]
    qkey: str
    question_open: bool
    ask: ModelOperatorQuestion | None = None


class ModelSharedBlockerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    judgements: tuple[ModelSharedCauseJudgement, ...]
    questions: tuple[ModelOperatorQuestion, ...]
    status_lines: tuple[str, ...] = Field(
        description="The lines queue-status prints, one per cause."
    )


class ModelAndonStartRequest(BaseModel):
    """One andon-start row, verbatim, and the checks the caller knows are shared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_text: str
    now: datetime
    shared_checks: tuple[str, ...]
    gated_env: str = "dev"
    max_reading_age_minutes: int = Field(default=60, gt=0)


class ModelAndonStartVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    on_shared_check: bool
    missing: tuple[str, ...]
    reasons: tuple[str, ...]
