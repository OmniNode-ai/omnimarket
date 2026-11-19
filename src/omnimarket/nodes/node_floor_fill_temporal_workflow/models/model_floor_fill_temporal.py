# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and outputs of workflow 2: the floor fill on a Temporal Schedule.

The workflow carries these models as JSON strings so its history holds exactly what was sent. Every step is a plain
command of a node; the command reads one ``ModelFloorFillStepInput`` on stdin and answers with one JSON line.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFloorFillTemporalSteps(_Frozen):
    """The argument vector of each step; every one names a node command, never inline logic."""

    read_pool: tuple[str, ...] = Field(min_length=1)
    run_fill: tuple[str, ...] = Field(min_length=1)
    read_claims: tuple[str, ...] = Field(min_length=1)
    write_alarm: tuple[str, ...] = Field(min_length=1)


class ModelFloorFillAlarmRule(_Frozen):
    """When an alarm row is written: free slots above the bar with nothing placed for N runs, or a lane not claiming."""

    free_slots_above: int = Field(default=5, ge=0)
    zero_runs: int = Field(default=2, gt=0)
    claim_window_seconds: int = Field(default=600, gt=0)


class ModelFloorFillTemporalPlan(_Frozen):
    """One scheduled run's input: the steps, the alarm rule, the claim poll and the retry limit."""

    steps: ModelFloorFillTemporalSteps
    rule: ModelFloorFillAlarmRule = ModelFloorFillAlarmRule()
    claim_poll_seconds: int = Field(default=60, gt=0)
    step_timeout_seconds: int = Field(default=600, gt=0)
    step_max_attempts: int = Field(default=3, gt=0, le=20)

    @model_validator(mode="after")
    def _poll_inside_window(self) -> ModelFloorFillTemporalPlan:
        if self.claim_poll_seconds > self.rule.claim_window_seconds:
            raise ValueError("the claim poll is no longer than the claim window")
        return self


class ModelFloorFillPoolAnswer(_Frozen):
    """The read_pool step: free lab Claude slots and unowned work, as the fill node reads them."""

    free_slots: int = Field(ge=0)
    unowned_work: int = Field(ge=0)
    detail: str = ""
    read_at: str = Field(min_length=1)
    # The cause when the fill could not read the pool: ``free_slots`` is then 0 only because there is no count.
    unread: str = ""


class ModelFloorFillRunAnswer(_Frozen):
    """The run_fill step: the lanes the one fill run placed, and why it placed none."""

    placed_lanes: tuple[str, ...] = ()
    why_none: str = ""
    free_slots: int | None = Field(default=None, ge=0)
    read_at: str = Field(min_length=1)
    # The sources the fill could not read, as the fill names them; a run with a cause here is not an idle run.
    unread: str = ""


class ModelFloorFillLaneClaim(_Frozen):
    """One placed lane's CLAIM row on the ledger."""

    lane: str = Field(min_length=1)
    claimed_at: str = Field(min_length=1)
    row: str = Field(min_length=1)


class ModelFloorFillClaimAnswer(_Frozen):
    """The read_claims step: which placed lanes wrote a CLAIM inside the window, and which did not."""

    claimed: tuple[ModelFloorFillLaneClaim, ...] = ()
    unclaimed: tuple[str, ...] = ()
    read_at: str = Field(min_length=1)


class ModelFloorFillStepInput(_Frozen):
    """What every step command reads on stdin."""

    step: str = Field(min_length=1)
    step_key: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    rule: ModelFloorFillAlarmRule
    pool: ModelFloorFillPoolAnswer | None = None
    fill: ModelFloorFillRunAnswer | None = None
    claims: ModelFloorFillClaimAnswer | None = None
    prior_zero_streak: int = Field(default=0, ge=0)
    window_closed: bool = False


class ModelFloorFillAlarmRequest(_Frozen):
    """The alarm rule's input: one run's readings, the streak carried from the last run, and the row cells."""

    rule: ModelFloorFillAlarmRule
    pool: ModelFloorFillPoolAnswer
    fill: ModelFloorFillRunAnswer
    claims: ModelFloorFillClaimAnswer | None = None
    prior_zero_streak: int = Field(default=0, ge=0)
    workflow_id: str = Field(min_length=1)
    step_key: str = Field(min_length=1)
    lane: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    ticket: str = Field(pattern=r"^OMN-\d+$")
    written_at: str = Field(min_length=1)


class ModelFloorFillAlarmDecision(_Frozen):
    """The streak after this run, the alarm keys raised and the rows to append (none when nothing is wrong)."""

    zero_streak: int = Field(ge=0)
    alarms: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()
    status_row: str = Field(min_length=1)


class ModelFloorFillAlarmAnswer(_Frozen):
    """The write_alarm step's answer: the decision and whether every row landed."""

    zero_streak: int = Field(ge=0)
    alarms: tuple[str, ...] = ()
    written: int = Field(ge=0)


class ModelFloorFillRunResult(_Frozen):
    """How one scheduled run ended; the next run reads ``zero_streak`` from it."""

    workflow_id: str
    pool: ModelFloorFillPoolAnswer
    fill: ModelFloorFillRunAnswer
    claims: ModelFloorFillClaimAnswer | None = None
    claim_readings: int = Field(ge=0)
    zero_streak: int = Field(ge=0)
    alarms: tuple[str, ...] = ()


class ModelFloorFillClaimReadRequest(_Frozen):
    """The claim reader's input: the ledger rows, the placed lanes and when they were placed."""

    rows: tuple[str, ...]
    lanes: tuple[str, ...]
    placed_at: str = Field(min_length=1)
    window_seconds: int = Field(gt=0)
    read_at: str = Field(min_length=1)


__all__: list[str] = [
    "ModelFloorFillAlarmAnswer",
    "ModelFloorFillAlarmDecision",
    "ModelFloorFillAlarmRequest",
    "ModelFloorFillAlarmRule",
    "ModelFloorFillClaimAnswer",
    "ModelFloorFillClaimReadRequest",
    "ModelFloorFillLaneClaim",
    "ModelFloorFillPoolAnswer",
    "ModelFloorFillRunAnswer",
    "ModelFloorFillRunResult",
    "ModelFloorFillStepInput",
    "ModelFloorFillTemporalPlan",
    "ModelFloorFillTemporalSteps",
]
