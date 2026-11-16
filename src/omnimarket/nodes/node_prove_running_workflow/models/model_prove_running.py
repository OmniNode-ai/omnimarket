# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs and outputs of workflow 1, prove-running.

The workflow carries these models as JSON strings so its history holds exactly what the caller sent. Every step is a
plain command (one of our nodes, through ``onex run-node`` or the node's declared CLI); the command reads one
``ModelProveRunningStepInput`` on stdin and answers with one JSON line on stdout.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.integration_reading import ModelIntegrationReadingBatch


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumProveRunningOutcome(StrEnum):
    """How one prove-running workflow ended."""

    PROVED = "proved"
    ALARM = "alarm"


class ModelProveRunningChange(_Frozen):
    """One merged change and the place it should run (a host or a runtime)."""

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0)
    merge_sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    target: str = Field(min_length=1, pattern=r"^\S+$")

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def workflow_id(self) -> str:
        """``<repo>#<n>@<sha>``: one workflow per change, so a change is never proven twice."""
        return f"{self.key}@{self.merge_sha}"


class ModelProveRunningSteps(_Frozen):
    """The argument vector of each step; every one names a node command, never inline logic."""

    read_status: tuple[str, ...] = Field(min_length=1)
    install: tuple[str, ...] = Field(min_length=1)
    read_running: tuple[str, ...] = Field(min_length=1)
    write_status: tuple[str, ...] = Field(min_length=1)
    write_alarm: tuple[str, ...] = Field(min_length=1)


class ModelProveRunningPlan(_Frozen):
    """One workflow's input: the change, its steps, the durable wait, the retry limit and the deadline."""

    change: ModelProveRunningChange
    steps: ModelProveRunningSteps
    wait_seconds: int = Field(gt=0)
    max_readings: int = Field(gt=0, le=100)
    deadline_seconds: int = Field(gt=0)
    step_timeout_seconds: int = Field(default=600, gt=0)
    step_max_attempts: int = Field(default=3, gt=0, le=20)

    @model_validator(mode="after")
    def _deadline_covers_one_wait(self) -> ModelProveRunningPlan:
        if self.deadline_seconds < self.wait_seconds:
            raise ValueError("the deadline is at least one durable wait long")
        return self


class ModelProveRunningAnswer(_Frozen):
    """What a read step answers: installed or not, running or not, where, at which revision, and what was seen."""

    installed: bool
    running: bool
    host: str = Field(min_length=1)
    revision: str = ""
    observed: str = ""
    detail: str = ""
    read_at: str = Field(min_length=1)


class ModelProveRunningStepInput(_Frozen):
    """What every step command reads on stdin."""

    step: str = Field(min_length=1)
    step_key: str = Field(min_length=1)
    change: ModelProveRunningChange
    last_reading: ModelProveRunningAnswer | None = None


class ModelProveRunningResult(_Frozen):
    """How a workflow ended, with the reading it ended on and how many readings it took."""

    workflow_id: str
    outcome: EnumProveRunningOutcome
    readings: int = Field(ge=0)
    installed_by_workflow: bool
    last_reading: ModelProveRunningAnswer | None = None


class ModelProveRunningRowRequest(_Frozen):
    """Build the ledger row for an ended workflow: a STATUS row when proved, an alarm row when not."""

    outcome: EnumProveRunningOutcome
    change: ModelProveRunningChange
    reading: ModelProveRunningAnswer | None
    step_key: str = Field(min_length=1)
    lane: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    ticket: str = Field(pattern=r"^OMN-\d+$")
    written_at: str = Field(min_length=1)


class ModelProveRunningRowResult(_Frozen):
    """The one ledger row the write step appends."""

    row: str = Field(min_length=1)


class ModelProveRunningStartRequest(_Frozen):
    """The starter's input: one integration pass's readings and how many NOT RUNNING lines to start."""

    batch: ModelIntegrationReadingBatch
    limit: int = Field(gt=0, le=50)


class ModelProveRunningStartBatch(_Frozen):
    """One start per NOT RUNNING line, oldest first, and the lines that could not be started with the reason."""

    changes: tuple[ModelProveRunningChange, ...]
    skipped: tuple[str, ...] = ()
